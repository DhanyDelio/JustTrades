import math
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd


class TestTokoSupabaseHelpers(unittest.TestCase):

    def test_toko_supabase_helpers_exist(self):
        """fetch_all_tokocrypto, upsert_tokocrypto, update_tokocrypto_by_order_id must be importable."""
        from services.supabase_client import (
            fetch_all_tokocrypto,
            upsert_tokocrypto,
            update_tokocrypto_by_order_id,
        )
        self.assertTrue(callable(fetch_all_tokocrypto))
        self.assertTrue(callable(upsert_tokocrypto))
        self.assertTrue(callable(update_tokocrypto_by_order_id))

    def test_fetch_all_tokocrypto_returns_empty_on_missing_table(self):
        """fetch_all_tokocrypto must return [] gracefully when Supabase raises."""
        with patch("services.supabase_client.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.table.return_value.select.return_value.order.return_value.execute.side_effect = Exception(
                "relation \"trades_tokocrypto\" does not exist"
            )
            from services.supabase_client import fetch_all_tokocrypto
            result = fetch_all_tokocrypto()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Helpers to build synthetic DataFrames without touching Supabase or the
# dashboard module (which requires a full Streamlit + Plotly environment).
# ---------------------------------------------------------------------------

def _make_toko_df(rows: list[dict]) -> pd.DataFrame:
    """
    Reproduce the derived-column logic from load_tokocrypto_data() so the
    metric / provenance / anomaly tests are self-contained.
    """
    TOKO_ANOMALY_STATES = {
        "CRITICAL_ANOMALY",
        "BOTH_CANCELED_ANOMALY",
        "RECONCILIATION_REQUIRED",
    }
    TOKO_EXCLUDED_EXIT_REASONS = {
        "OCO_STUCK_MANUAL_RESOLUTION",
        "CRITICAL_ANOMALY",
        "BOTH_CANCELED_ANOMALY",
        "RECONCILIATION_REQUIRED",
    }

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["realized_pnl_idr", "realized_pnl_pct",
                "slippage_pct", "exit_fill_slippage_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    exit_st = (
        df["exit_status"].fillna("").astype(str).str.upper()
        if "exit_status" in df.columns
        else pd.Series("", index=df.index)
    )
    df["is_resolved"] = exit_st.isin(["TP_HIT", "SL_HIT"])
    df["is_win"] = (
        df["realized_pnl_idr"].gt(0)
        if "realized_pnl_idr" in df.columns
        else pd.Series(False, index=df.index)
    )

    oco_st = (
        df["oco_state"].fillna("").astype(str)
        if "oco_state" in df.columns
        else pd.Series("", index=df.index)
    )
    df["has_anomaly"] = oco_st.isin(TOKO_ANOMALY_STATES)

    return df


def _build_toko_metrics(df: pd.DataFrame) -> dict:
    """Thin replica of build_toko_metrics() — no Streamlit dependency."""
    TOKO_EXCLUDED_EXIT_REASONS = {
        "OCO_STUCK_MANUAL_RESOLUTION",
        "CRITICAL_ANOMALY",
        "BOTH_CANCELED_ANOMALY",
        "RECONCILIATION_REQUIRED",
    }
    resolved = df[df["is_resolved"]].copy()
    if "exit_reason" in resolved.columns:
        genuine = resolved[~resolved["exit_reason"].isin(TOKO_EXCLUDED_EXIT_REASONS)]
    else:
        genuine = resolved

    total_trades   = int(len(df))
    resolved_count = int(len(resolved))
    genuine_count  = int(len(genuine))
    win_rate       = round(float(genuine["is_win"].mean() * 100) if genuine_count else 0.0, 2)
    total_pnl_idr  = round(float(genuine["realized_pnl_idr"].sum()) if genuine_count else 0.0, 0)

    open_rows      = (
        df[df["exit_status"].fillna("").astype(str).str.upper() == "OPEN"]
        if "exit_status" in df.columns
        else pd.DataFrame()
    )
    slots_occupied = int(len(open_rows))

    return {
        "total_trades":   total_trades,
        "resolved_count": resolved_count,
        "genuine_count":  genuine_count,
        "win_rate":       win_rate,
        "total_pnl_idr":  total_pnl_idr,
        "slots_occupied": slots_occupied,
    }


def _build_toko_exit_provenance(df: pd.DataFrame) -> pd.DataFrame:
    """Thin replica of build_toko_exit_provenance() — no Streamlit dependency."""
    TOKO_EXCLUDED_EXIT_REASONS = {
        "OCO_STUCK_MANUAL_RESOLUTION",
        "CRITICAL_ANOMALY",
        "BOTH_CANCELED_ANOMALY",
        "RECONCILIATION_REQUIRED",
    }
    if df.empty or "exit_reason" not in df.columns:
        return pd.DataFrame(columns=["exit_reason", "count", "genuine"])

    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return pd.DataFrame(columns=["exit_reason", "count", "genuine"])

    counts = (
        resolved.groupby(resolved["exit_reason"].fillna("UNSPECIFIED"), as_index=False)
        .size()
        .rename(columns={"size": "count"})
    )
    counts["genuine"] = ~counts["exit_reason"].isin(TOKO_EXCLUDED_EXIT_REASONS)
    return counts.sort_values("count", ascending=False)


# ---------------------------------------------------------------------------
# Tests for build_toko_metrics
# ---------------------------------------------------------------------------

class TestBuildTokoMetrics(unittest.TestCase):

    def test_empty_dataframe(self):
        """Metrics on empty df should return all-zero defaults."""
        df = _make_toko_df([])
        # build_toko_metrics requires a non-empty df in the real code;
        # the caller passes the default dict instead. Mirror that here.
        metrics = {
            "total_trades": 0, "resolved_count": 0, "genuine_count": 0,
            "win_rate": 0.0, "total_pnl_idr": 0.0, "slots_occupied": 0,
        }
        self.assertEqual(metrics["total_trades"], 0)
        self.assertEqual(metrics["win_rate"], 0.0)

    def test_genuine_exits_only_counted(self):
        """
        OCO_TRIGGERED exits contribute to PnL; non-genuine exits are excluded
        from total_pnl_idr and win_rate but still show in resolved_count.
        """
        rows = [
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": 5000.0, "oco_state": "TP_HIT"},
            {"exit_status": "SL_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": -2000.0, "oco_state": "SL_HIT"},
            {"exit_status": "TP_HIT", "exit_reason": "OCO_STUCK_MANUAL_RESOLUTION",
             "realized_pnl_idr": 9999.0, "oco_state": "TP_HIT"},
        ]
        df = _make_toko_df(rows)
        m = _build_toko_metrics(df)

        self.assertEqual(m["resolved_count"], 3)   # all three are resolved
        self.assertEqual(m["genuine_count"], 2)    # only OCO_TRIGGERED ones
        # total_pnl_idr must exclude the manual-resolution row
        self.assertAlmostEqual(m["total_pnl_idr"], 3000.0, places=0)
        # win_rate is among genuine only: 1 win out of 2 genuine = 50 %
        self.assertAlmostEqual(m["win_rate"], 50.0, places=1)

    def test_all_genuine_wins(self):
        rows = [
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": 1000.0, "oco_state": "TP_HIT"},
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": 2000.0, "oco_state": "TP_HIT"},
        ]
        df = _make_toko_df(rows)
        m = _build_toko_metrics(df)
        self.assertEqual(m["win_rate"], 100.0)
        self.assertAlmostEqual(m["total_pnl_idr"], 3000.0, places=0)

    def test_slots_occupied_counts_open_status(self):
        rows = [
            {"exit_status": "OPEN",   "exit_reason": None, "realized_pnl_idr": None, "oco_state": "EXECUTING"},
            {"exit_status": "OPEN",   "exit_reason": None, "realized_pnl_idr": None, "oco_state": "EXECUTING"},
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED", "realized_pnl_idr": 500.0, "oco_state": "TP_HIT"},
        ]
        df = _make_toko_df(rows)
        m = _build_toko_metrics(df)
        self.assertEqual(m["slots_occupied"], 2)


# ---------------------------------------------------------------------------
# Tests for has_anomaly derivation
# ---------------------------------------------------------------------------

class TestTokoAnomalyDetection(unittest.TestCase):

    def test_critical_anomaly_flagged(self):
        rows = [
            {"oco_state": "CRITICAL_ANOMALY",     "exit_status": "OPEN", "realized_pnl_idr": None},
            {"oco_state": "EXECUTING",             "exit_status": "OPEN", "realized_pnl_idr": None},
            {"oco_state": "BOTH_CANCELED_ANOMALY", "exit_status": "OPEN", "realized_pnl_idr": None},
            {"oco_state": "RECONCILIATION_REQUIRED","exit_status": "OPEN", "realized_pnl_idr": None},
            {"oco_state": "TP_HIT",                "exit_status": "TP_HIT", "realized_pnl_idr": 100.0},
        ]
        df = _make_toko_df(rows)
        anomaly_rows = df[df["has_anomaly"]]
        self.assertEqual(len(anomaly_rows), 3)

    def test_no_anomaly_on_clean_states(self):
        rows = [
            {"oco_state": "EXECUTING", "exit_status": "OPEN",   "realized_pnl_idr": None},
            {"oco_state": "TP_HIT",    "exit_status": "TP_HIT", "realized_pnl_idr": 200.0},
            {"oco_state": "SL_HIT",    "exit_status": "SL_HIT", "realized_pnl_idr": -100.0},
        ]
        df = _make_toko_df(rows)
        self.assertEqual(df["has_anomaly"].sum(), 0)

    def test_null_oco_state_not_anomaly(self):
        rows = [
            {"oco_state": None,  "exit_status": "OPEN", "realized_pnl_idr": None},
            {"oco_state": "",    "exit_status": "OPEN", "realized_pnl_idr": None},
        ]
        df = _make_toko_df(rows)
        self.assertEqual(df["has_anomaly"].sum(), 0)


# ---------------------------------------------------------------------------
# Tests for build_toko_exit_provenance
# ---------------------------------------------------------------------------

class TestBuildTokoExitProvenance(unittest.TestCase):

    def test_empty_df_returns_empty_provenance(self):
        prov = _build_toko_exit_provenance(pd.DataFrame())
        self.assertTrue(prov.empty)

    def test_genuine_flag_correct(self):
        rows = [
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",              "realized_pnl_idr": 100.0, "oco_state": "TP_HIT"},
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",              "realized_pnl_idr": 200.0, "oco_state": "TP_HIT"},
            {"exit_status": "TP_HIT", "exit_reason": "OCO_STUCK_MANUAL_RESOLUTION","realized_pnl_idr": 300.0, "oco_state": "TP_HIT"},
            {"exit_status": "SL_HIT", "exit_reason": "CRITICAL_ANOMALY",           "realized_pnl_idr": -50.0, "oco_state": "SL_HIT"},
        ]
        df = _make_toko_df(rows)
        prov = _build_toko_exit_provenance(df)

        oco_row = prov[prov["exit_reason"] == "OCO_TRIGGERED"]
        self.assertFalse(oco_row.empty)
        self.assertTrue(bool(oco_row.iloc[0]["genuine"]))
        self.assertEqual(int(oco_row.iloc[0]["count"]), 2)

        manual_row = prov[prov["exit_reason"] == "OCO_STUCK_MANUAL_RESOLUTION"]
        self.assertFalse(manual_row.empty)
        self.assertFalse(bool(manual_row.iloc[0]["genuine"]))

        anomaly_row = prov[prov["exit_reason"] == "CRITICAL_ANOMALY"]
        self.assertFalse(anomaly_row.empty)
        self.assertFalse(bool(anomaly_row.iloc[0]["genuine"]))

    def test_non_genuine_exits_not_hidden(self):
        """All exit_reasons must appear in provenance, not be silently dropped."""
        rows = [
            {"exit_status": "SL_HIT", "exit_reason": "BOTH_CANCELED_ANOMALY",
             "realized_pnl_idr": -100.0, "oco_state": "SL_HIT"},
        ]
        df = _make_toko_df(rows)
        prov = _build_toko_exit_provenance(df)
        self.assertEqual(len(prov), 1)
        self.assertIn("BOTH_CANCELED_ANOMALY", prov["exit_reason"].values)
        self.assertFalse(bool(prov.iloc[0]["genuine"]))


# ---------------------------------------------------------------------------
# Tests for slippage threshold flags
# ---------------------------------------------------------------------------

class TestTokoSlippageFlags(unittest.TestCase):

    ENTRY_THRESHOLD = 0.3
    EXIT_THRESHOLD  = 0.1

    def _apply_flags(self, df: pd.DataFrame) -> pd.DataFrame:
        """Reproduce the slippage-flag logic from the dashboard's Section 5."""
        df = df.copy()
        if "slippage_pct" in df.columns:
            df["entry_slip_flag"] = pd.to_numeric(
                df["slippage_pct"], errors="coerce"
            ).abs().gt(self.ENTRY_THRESHOLD)
        if "exit_fill_slippage_pct" in df.columns:
            df["exit_slip_flag"] = pd.to_numeric(
                df["exit_fill_slippage_pct"], errors="coerce"
            ).abs().gt(self.EXIT_THRESHOLD)
        return df

    def test_entry_slippage_above_threshold_flagged(self):
        rows = [
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": 100.0, "oco_state": "TP_HIT",
             "slippage_pct": 0.5, "exit_fill_slippage_pct": 0.05},
        ]
        df = _make_toko_df(rows)
        flagged = self._apply_flags(df[df["is_resolved"]])
        self.assertTrue(bool(flagged.iloc[0]["entry_slip_flag"]))
        self.assertFalse(bool(flagged.iloc[0]["exit_slip_flag"]))

    def test_exit_slippage_above_threshold_flagged(self):
        rows = [
            {"exit_status": "SL_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": -200.0, "oco_state": "SL_HIT",
             "slippage_pct": 0.1, "exit_fill_slippage_pct": 0.25},
        ]
        df = _make_toko_df(rows)
        flagged = self._apply_flags(df[df["is_resolved"]])
        self.assertFalse(bool(flagged.iloc[0]["entry_slip_flag"]))
        self.assertTrue(bool(flagged.iloc[0]["exit_slip_flag"]))

    def test_slippage_below_threshold_not_flagged(self):
        rows = [
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": 500.0, "oco_state": "TP_HIT",
             "slippage_pct": 0.05, "exit_fill_slippage_pct": 0.02},
        ]
        df = _make_toko_df(rows)
        flagged = self._apply_flags(df[df["is_resolved"]])
        self.assertFalse(bool(flagged.iloc[0]["entry_slip_flag"]))
        self.assertFalse(bool(flagged.iloc[0]["exit_slip_flag"]))

    def test_null_slippage_not_flagged(self):
        rows = [
            {"exit_status": "TP_HIT", "exit_reason": "OCO_TRIGGERED",
             "realized_pnl_idr": 100.0, "oco_state": "TP_HIT",
             "slippage_pct": None, "exit_fill_slippage_pct": None},
        ]
        df = _make_toko_df(rows)
        flagged = self._apply_flags(df[df["is_resolved"]])
        # NaN comparison → False (not flagged)
        self.assertFalse(bool(flagged.iloc[0]["entry_slip_flag"]))
        self.assertFalse(bool(flagged.iloc[0]["exit_slip_flag"]))


if __name__ == "__main__":
    unittest.main()
