import math
import os
import pytz
from datetime import datetime, timedelta
import streamlit.components.v1 as components

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import numpy as np
import plotly.express as px
import streamlit as st
from pathlib import Path

from services.supabase_client import fetch_all_spot, fetch_all_futures
from streamlit_autorefresh import st_autorefresh


st.set_page_config(page_title="Swing Trade Dashboard", layout="wide")

import threading
import asyncio
import time
# SDK v2.x: create_async_client is exported from top-level supabase package.
# supabase._async.client exposes create_client (not create_async_client) internally,
# so always import from the top-level package to avoid ImportError.
try:
    from supabase import create_async_client
    from supabase._async.client import AsyncClient  # type annotation only
except ImportError:
    from supabase import create_async_client  # type: ignore

class DashboardState:
    def __init__(self):
        self.spot_rows = None
        self.futures_rows = None
        self.last_updated = time.time()
        self.last_event_time = None  # HH:MM:SS string of last WebSocket event
        self.connected = False
        self.lock = threading.Lock()
        self.pending_rerun = False   # set True by WS callbacks, cleared after rerun

@st.cache_resource
def get_global_state():
    state = DashboardState()
    # --- Direct initial fetch on first load ---
    # Populate state immediately via blocking REST call so the UI never
    # shows "Waiting..." when historical data already exists in the DB.
    try:
        from services.supabase_client import fetch_all_spot, fetch_all_futures
        state.spot_rows    = fetch_all_spot()
        state.futures_rows = fetch_all_futures()
        state.last_updated = time.time()

        # Hydrate last_event_time from the most recent updated_at / created_at
        # across both tables so the status bar shows a real timestamp instantly.
        _best_ts: float | None = None
        for row in (state.spot_rows or []) + (state.futures_rows or []):
            for col in ("updated_at", "created_at"):
                raw = row.get(col)
                if not raw:
                    continue
                try:
                    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    ts = dt.timestamp()
                    if _best_ts is None or ts > _best_ts:
                        _best_ts = ts
                        state.last_event_time = (
                            dt.astimezone(pytz.timezone("Asia/Jakarta"))
                            .strftime("%H:%M:%S")
                        )
                except Exception:
                    pass
    except Exception:
        # If the DB is unreachable at startup, fall through gracefully;
        # load_trade_data() / load_futures_data() will retry on first render.
        pass
    return state

@st.cache_resource
def start_realtime_listener(_state: DashboardState):
    def run_async_loop():
        asyncio.run(realtime_loop(_state))
    
    t = threading.Thread(target=run_async_loop, daemon=True)
    t.start()
    return t

def _extract_event_time(record: dict) -> str:
    """Return HH:MM:SS WIB string from updated_at / created_at, or current time."""
    wib = pytz.timezone("Asia/Jakarta")
    for col in ("updated_at", "created_at"):
        raw = record.get(col)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt.astimezone(wib).strftime("%H:%M:%S")
            except Exception:
                pass
    return datetime.now(wib).strftime("%H:%M:%S")


async def realtime_loop(state: DashboardState):
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return

    # ── Helper: push rerun request to every active Streamlit session ──
    def trigger_ui_update():
        # Mark that new data arrived — main() checks this flag at end of render
        # to decide whether to rerun. Using a boolean flag is more reliable than
        # timestamp comparison (avoids float precision / same-tick issues).
        state.pending_rerun = True
        try:
            from streamlit.runtime import get_instance
            runtime = get_instance()
            list_fn = getattr(
                runtime._session_mgr,
                "list_active_sessions",
                getattr(runtime._session_mgr, "list_sessions", None),
            )
            if list_fn is None:
                return
            for session_info in list_fn():
                if hasattr(session_info.session, "request_rerun"):
                    try:
                        session_info.session.request_rerun(None)
                    except TypeError:
                        session_info.session.request_rerun()
        except Exception:
            pass

    # ── Generic upsert helper shared by both callbacks ─────────────────
    def _upsert_row(rows: list[dict], record: dict) -> list[dict]:
        """Return a new list with record inserted or updated in-place."""
        for i, row in enumerate(rows):
            if row.get("id") == record.get("id"):
                rows[i] = record
                return rows
        rows.append(record)
        return rows

    # ── Callbacks: handle INSERT *and* UPDATE from postgres_changes ────
    def on_spot_change(payload):
        record = payload.get("record") or payload.get("new")
        if not record:
            return
        with state.lock:
            if state.spot_rows is None:
                state.spot_rows = []
            state.spot_rows = _upsert_row(state.spot_rows, record)
            state.last_updated  = time.time()
            state.last_event_time = _extract_event_time(record)
        trigger_ui_update()

    def on_futures_change(payload):
        record = payload.get("record") or payload.get("new")
        if not record:
            return
        with state.lock:
            if state.futures_rows is None:
                state.futures_rows = []
            state.futures_rows = _upsert_row(state.futures_rows, record)
            state.last_updated    = time.time()
            state.last_event_time = _extract_event_time(record)
        trigger_ui_update()

    # ── Reconnect loop — if WS drops, wait 5s and reconnect ───────────
    RETRY_DELAY = 5  # seconds

    while True:
        try:
            # SDK v2.x: create_async_client from supabase._async.client
            client = await create_async_client(url, key)

            # Subscribe to trades_spot — capture INSERT and UPDATE
            # on_postgres_changes signature: (event, callback, table=None, schema=None)
            # callback is positional arg 2 — must come before table/schema keywords
            channel_spot = client.channel("public:trades_spot")
            channel_spot.on_postgres_changes(
                event="*",          # catches INSERT + UPDATE + DELETE
                callback=on_spot_change,
                schema="public",
                table="trades_spot",
            )

            # Subscribe to trades_futures — capture INSERT and UPDATE
            channel_futures = client.channel("public:trades_futures")
            channel_futures.on_postgres_changes(
                event="*",
                callback=on_futures_change,
                schema="public",
                table="trades_futures",
            )

            # SDK v2.x subscribe() is a coroutine — must be awaited
            await channel_spot.subscribe()
            await channel_futures.subscribe()
            state.connected = True

            # Keep the loop alive; sleep in small increments so we can
            # detect a future cancellation quickly.
            while True:
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            state.connected = False
            return
        except Exception:
            state.connected = False
            await asyncio.sleep(RETRY_DELAY)

global_state = get_global_state()
start_realtime_listener(global_state)


WIB = pytz.timezone("Asia/Jakarta")
VM_STALL_THRESHOLD_SECONDS = 70 * 60
VM_DOWN_REFRESH_MS = 300_000
NORMAL_REFRESH_BUFFER_SECONDS = 5


def _parse_iso_to_wib(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(WIB)
    except Exception:
        return None


def _get_next_dashboard_refresh(now_wib: datetime) -> datetime:
    return (now_wib + timedelta(hours=1)).replace(
        minute=0,
        second=NORMAL_REFRESH_BUFFER_SECONDS,
        microsecond=0,
    )


def _get_autorefresh_interval_ms(now_wib: datetime, is_vm_down: bool) -> int:
    if is_vm_down:
        return VM_DOWN_REFRESH_MS
    target_wib = _get_next_dashboard_refresh(now_wib)
    seconds_until_next_cycle = max(1, math.ceil((target_wib - now_wib).total_seconds()))
    return seconds_until_next_cycle * 1000


STARTING_LAB_CAPITAL = 240.0


# ---------------------------------------------------------------------------
# SPOT — data loading + helpers
# ---------------------------------------------------------------------------

def _sync_session_state():
    """
    Detect when the Realtime listener has pushed new data (pending_rerun=True)
    and ensure the current Streamlit session picks it up.
    """
    seen_ts = st.session_state.get("_last_seen_update", 0.0)
    if global_state.last_updated > seen_ts:
        st.session_state["_last_seen_update"] = global_state.last_updated

_sync_session_state()


def load_trade_data() -> pd.DataFrame:
    with global_state.lock:
        if global_state.spot_rows is None:
            try:
                global_state.spot_rows = fetch_all_spot()
            except Exception as exc:
                st.error("Failed to load spot trades from Supabase.")
                st.exception(exc)
                return pd.DataFrame()
        rows = list(global_state.spot_rows)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["realized_pnl_usd", "realized_pnl_pct", "planned_rr",
                "entry_price", "entry_fill_price", "sl", "tp1",
                "entry_qty", "entry_notional", "budget_usd", "ml_score"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["is_resolved"] = df["exit_status"].fillna("").astype(str).str.upper().isin(["TP_HIT", "SL_HIT", "CANCELED"])
    df["is_win"]      = df["realized_pnl_usd"].gt(0)

    def parse_epoch_ms(series):
        values = pd.to_numeric(series, errors="coerce")
        return pd.to_datetime(values, unit="ms", utc=True, errors="coerce")

    df["entry_fill_dt"] = parse_epoch_ms(df.get("entry_fill_time"))
    df["exit_dt"]       = parse_epoch_ms(df.get("exit_time"))
    df["entry_fill_wib"] = (df["entry_fill_dt"].dt.tz_convert("Asia/Jakarta")
                            if "entry_fill_dt" in df.columns
                            else pd.Series(dtype="datetime64[ns, UTC]"))
    df["entry_hour"]    = (df["entry_fill_wib"].dt.hour
                           if "entry_fill_wib" in df.columns
                           else pd.Series(dtype="float64"))

    df["zone_touches_num"]   = pd.to_numeric(df.get("zone_touches"), errors="coerce")
    df["zone_strength"]      = df["zone_touches_num"].fillna(0)
    df["zone_strength_label"] = pd.cut(
        df["zone_strength"],
        bins=[-1, 1, 2, 3, float("inf")],
        labels=["1x", "2x", "3x", "4x+"],
        right=True, include_lowest=True,
    )

    df["cluster_mode"] = df["correlation_cluster_id"].notna().map(
        {True: "lab", False: "single"}
    ).fillna("single")

    return df


def build_metrics(df: pd.DataFrame):
    resolved        = df[df["is_resolved"]].copy()
    total_trades    = int(len(df))
    resolved_trades = int(len(resolved))
    win_rate        = round((resolved["is_win"].mean() * 100) if resolved_trades else 0.0, 2)
    total_realized_pnl = round(float(resolved["realized_pnl_usd"].sum()) if resolved_trades else 0.0, 2)

    cluster_mask = df["correlation_cluster_id"].notna()
    cluster_pnl  = float(df.loc[cluster_mask & df["is_resolved"], "realized_pnl_usd"].sum()) if cluster_mask.any() else 0.0
    lab_capital  = STARTING_LAB_CAPITAL + cluster_pnl

    cluster_ids  = resolved["correlation_cluster_id"].dropna().unique()
    effective_n  = len(cluster_ids) + int((resolved["correlation_cluster_id"].isna()).sum())

    return {
        "total_trades":        total_trades,
        "resolved_trades":     resolved_trades,
        "win_rate":            win_rate,
        "total_realized_pnl":  total_realized_pnl,
        "lab_capital":         round(lab_capital, 2),
        "effective_n":         effective_n,
        "raw_trade_count":     total_trades,
    }


def build_equity_curve(df: pd.DataFrame):
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    data = []
    for mode in ["single", "lab"]:
        subset = resolved[resolved["cluster_mode"] == mode].copy()
        if subset.empty:
            continue
        subset = subset.sort_values("exit_dt")
        subset["cumulative_pnl"] = subset["realized_pnl_usd"].cumsum()
        subset["mode"] = mode.title()
        data.append(subset[["exit_dt", "cumulative_pnl", "mode"]])

    if not data:
        return None

    chart_df = pd.concat(data, ignore_index=True)
    fig = px.line(
        chart_df, x="exit_dt", y="cumulative_pnl", color="mode", markers=True,
        labels={"exit_dt": "Exit time", "cumulative_pnl": "Cumulative PnL ($)", "mode": "Mode"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    return fig


def build_symbol_pnl(df: pd.DataFrame):
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    summary = (
        resolved.groupby("symbol", as_index=False)
        .agg(realized_pnl_usd=("realized_pnl_usd", "sum"), win=("is_win", "mean"))
    )
    summary["win_label"] = summary["win"].ge(0.5)
    fig = px.bar(
        summary, x="symbol", y="realized_pnl_usd", color="win_label",
        color_discrete_map={True: "#2ca02c", False: "#d62728"},
        labels={"symbol": "Symbol", "realized_pnl_usd": "Realized PnL ($)", "win_label": "Win"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20), xaxis_tickangle=-30)
    return fig


def build_hourly_charts(df: pd.DataFrame):
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None, None

    hourly = (
        resolved.groupby("entry_hour", as_index=False)
        .agg(win_rate=("is_win", "mean"), avg_realized_pnl_pct=("realized_pnl_pct", "mean"),
             trades=("symbol", "count"))
        .sort_values("entry_hour")
    )
    hourly["entry_hour"]   = hourly["entry_hour"].fillna(-1).astype(int)
    hourly["win_rate_pct"] = hourly["win_rate"] * 100
    hourly["count_label"]  = hourly["trades"].apply(lambda n: f"{n}t")

    win_fig = px.bar(
        hourly, x="entry_hour", y="win_rate_pct", text="count_label",
        labels={"entry_hour": "Hour (WIB/UTC+7)", "win_rate_pct": "Win rate (%)"},
        hover_data={"trades": True, "win_rate_pct": ":.1f"},
    )
    win_fig.update_traces(textposition="outside")
    win_fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    win_fig.update_yaxes(range=[0, 110])

    pnl_fig = px.bar(
        hourly, x="entry_hour", y="avg_realized_pnl_pct", text="count_label",
        labels={"entry_hour": "Hour (WIB/UTC+7)", "avg_realized_pnl_pct": "Avg PnL (%)"},
        hover_data={"trades": True},
    )
    pnl_fig.update_traces(textposition="outside")
    pnl_fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    return win_fig, pnl_fig


def build_rr_scatter(df: pd.DataFrame):
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    fig = px.scatter(
        resolved, x="planned_rr", y="realized_pnl_pct", color="is_win",
        color_discrete_map={True: "#2ca02c", False: "#d62728"},
        hover_name="symbol",
        labels={"planned_rr": "Planned R:R", "realized_pnl_pct": "Realized PnL (%)", "is_win": "Win"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    return fig


def build_zone_strength(df: pd.DataFrame):
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    summary = (
        resolved.groupby("zone_strength_label", as_index=False)
        .agg(win_rate=("is_win", "mean"), trades=("symbol", "count"))
        .sort_values("zone_strength_label")
    )
    summary["win_rate_pct"] = summary["win_rate"] * 100
    fig = px.bar(
        summary, x="zone_strength_label", y="win_rate_pct",
        labels={"zone_strength_label": "Zone touches", "win_rate_pct": "Win rate (%)"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    fig.update_yaxes(range=[0, 100])
    return fig


def build_cluster_breakdown(df: pd.DataFrame):
    if df.empty:
        return pd.DataFrame(columns=["cluster_id", "trades", "resolved", "aggregate_pnl_usd"])

    summary = []
    for cluster_id, group in df.groupby(df["correlation_cluster_id"].fillna("single")):
        resolved_group = group[group["is_resolved"]]
        summary.append({
            "cluster_id":        cluster_id,
            "trades":            int(len(group)),
            "resolved":          int(len(resolved_group)),
            "aggregate_pnl_usd": round(float(resolved_group["realized_pnl_usd"].sum()), 2),
        })
    return pd.DataFrame(summary).sort_values("aggregate_pnl_usd", ascending=False)


# ---------------------------------------------------------------------------
# FUTURES — data loading + helpers
# ---------------------------------------------------------------------------

def load_futures_data() -> pd.DataFrame:
    """Load futures trades from Supabase (trades_futures table). Returns empty DataFrame on error."""
    with global_state.lock:
        if global_state.futures_rows is None:
            try:
                global_state.futures_rows = fetch_all_futures()
            except Exception as exc:
                st.error("Failed to load futures trades from Supabase.")
                st.exception(exc)
                return pd.DataFrame()
        rows = list(global_state.futures_rows)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["realized_pnl_usd", "realized_pnl_pct", "planned_rr",
                "entry_price", "entry_fill_price", "sl", "tp1",
                "entry_qty", "entry_notional", "margin_used",
                "leverage", "liquidation_price",
                "distance_to_liquidation_pct", "funding_rate_paid"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["is_resolved"] = df["exit_status"].fillna("").astype(str).str.upper().isin(["TP_HIT", "SL_HIT", "CANCELED"])
    df["is_win"]      = df["realized_pnl_usd"].gt(0)

    def parse_epoch_ms(series):
        values = pd.to_numeric(series, errors="coerce")
        return pd.to_datetime(values, unit="ms", utc=True, errors="coerce")

    df["exit_dt"] = parse_epoch_ms(df.get("exit_time"))

    if "position_side" not in df.columns:
        df["position_side"] = "UNKNOWN"

    return df


def build_futures_side_stats(df: pd.DataFrame) -> list[dict]:
    """
    Compute per-side stats (LONG / SHORT) matching cmd_stats_futures logic exactly.
    effective_n = unique cluster sessions + singles (cluster-based, same as spot cmd_stats).
    Trades from --propose-multi share a correlation_cluster_id and co-move, so they
    count as ONE independent observation per cluster, not N raw trades.
    z-score uses raw n for the binomial formula (sample size), but effective_n is
    reported separately to flag independence caveat.
    """
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return []

    groups = resolved.groupby(
        [resolved["rule_version"].fillna("unknown"),
         resolved["position_side"].fillna("UNKNOWN")]
    )

    results = []
    for (version, ps), group in groups:
        n        = len(group)
        wins     = group[group["exit_status"].str.upper() == "TP_HIT"]
        losses   = group[group["exit_status"].str.upper() == "SL_HIT"]
        win_rate = len(wins) / n if n > 0 else 0.0

        avg_rr      = group["planned_rr"].mean() if n > 0 else 0.0
        be_win_rate = 1 / (1 + avg_rr) if avg_rr > 0 else 0.5
        avg_win_pct  = float(wins["realized_pnl_pct"].mean())  if len(wins)   else 0.0
        avg_loss_pct = float(losses["realized_pnl_pct"].mean()) if len(losses) else 0.0
        avg_fee_pct  = float(
            (group["fee_usd_roundtrip"].fillna(0) /
             group["entry_notional"].fillna(1).clip(lower=0.001) * 100).mean()
        ) if n > 0 else 0.0
        expectancy  = (win_rate * avg_win_pct) - ((1 - win_rate) * abs(avg_loss_pct)) - avg_fee_pct

        if n >= 2:
            p0 = be_win_rate
            z  = (win_rate - p0) / math.sqrt(p0 * (1 - p0) / n)
            if abs(z) >= 1.96:
                sig = "✅ p<0.05"
            elif abs(z) >= 1.645:
                sig = "🟡 p<0.10"
            else:
                sig = "⚠ not sig"
        else:
            z, sig = 0.0, "⚠ n/a"

        # Cluster-based effective-n: same logic as spot cmd_stats
        cluster_col = group.get("correlation_cluster_id") if "correlation_cluster_id" in group.columns else None
        if cluster_col is not None:
            cluster_ids = set(cluster_col.dropna().unique())
            n_clusters  = len(cluster_ids)
            n_singles   = int((cluster_col.isna()).sum())
        else:
            n_clusters = 0
            n_singles  = n
        effective_n = n_clusters + n_singles

        results.append({
            "rule_version":  version,
            "side":          ps,
            "n":             n,
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(win_rate * 100, 1),
            "be_win_rate":   round(be_win_rate * 100, 1),
            "avg_rr":        round(avg_rr, 2),
            "avg_win_pct":   round(avg_win_pct, 2),
            "avg_loss_pct":  round(avg_loss_pct, 2),
            "expectancy":    round(expectancy, 3),
            "z_score":       round(z, 2),
            "significance":  sig,
            "effective_n":   effective_n,
            "n_clusters":    n_clusters,
            "n_singles":     n_singles,
        })

    return results


def build_futures_equity(df: pd.DataFrame):
    """Equity curve by position side (LONG / SHORT)."""
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    data = []
    for side in resolved["position_side"].dropna().unique():
        subset = resolved[resolved["position_side"] == side].copy()
        if subset.empty:
            continue
        subset = subset.sort_values("exit_dt")
        subset["cumulative_pnl"] = subset["realized_pnl_usd"].cumsum()
        subset["Side"] = side
        data.append(subset[["exit_dt", "cumulative_pnl", "Side"]])

    if not data:
        return None

    chart_df = pd.concat(data, ignore_index=True)
    fig = px.line(
        chart_df, x="exit_dt", y="cumulative_pnl", color="Side", markers=True,
        labels={"exit_dt": "Exit time", "cumulative_pnl": "Cumulative PnL ($)"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    return fig


def build_futures_symbol_pnl(df: pd.DataFrame):
    """Win/loss PnL bar grouped by symbol + side."""
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    resolved["sym_side"] = resolved["symbol"] + " " + resolved["position_side"].fillna("")
    summary = (
        resolved.groupby("sym_side", as_index=False)
        .agg(realized_pnl_usd=("realized_pnl_usd", "sum"), win=("is_win", "mean"))
    )
    summary["win_label"] = summary["win"].ge(0.5)
    fig = px.bar(
        summary, x="sym_side", y="realized_pnl_usd", color="win_label",
        color_discrete_map={True: "#2ca02c", False: "#d62728"},
        labels={"sym_side": "Symbol (side)", "realized_pnl_usd": "Realized PnL ($)", "win_label": "Win"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20), xaxis_tickangle=-30)
    return fig


def build_futures_rr_scatter(df: pd.DataFrame):
    """Planned R:R vs realized PnL % coloured by side."""
    resolved = df[df["is_resolved"]].copy()
    if resolved.empty:
        return None

    fig = px.scatter(
        resolved, x="planned_rr", y="realized_pnl_pct",
        color="position_side",
        symbol="is_win",
        symbol_map={True: "circle", False: "x"},
        hover_name="symbol",
        labels={"planned_rr": "Planned R:R", "realized_pnl_pct": "Realized PnL (%)",
                "position_side": "Side", "is_win": "Win"},
    )
    fig.update_layout(template="plotly_white", margin=dict(l=20, r=20, t=40, b=20))
    return fig


def render_futures_side_stats(side_stats: list[dict]) -> None:
    """Render per-side stat cards in the Futures tab."""
    if not side_stats:
        st.info("No closed futures trades yet.")
        return

    for s in side_stats:
        with st.expander(
            f"**{s['side']}**  —  rule `{s['rule_version']}`  "
            f"|  {s['wins']}W / {s['losses']}L  |  {s['significance']}",
            expanded=True,
        ):
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Win rate",       f"{s['win_rate']:.1f}%",
                      delta=f"B/E {s['be_win_rate']:.1f}%")
            c2.metric("Avg R:R",        f"{s['avg_rr']:.2f}:1")
            c3.metric("Expectancy",     f"{s['expectancy']:+.3f}%")
            c4.metric("Z-score",        f"{s['z_score']:+.2f}")
            c5.metric("Effective N",    s["effective_n"],
                      delta=f"raw {s['n']}" if s["effective_n"] != s["n"] else None)
            if s["n"] < 30:
                st.caption(f"⚠ Only {s['n']} trades — z-score unreliable until n ≥ 30")
            # Cluster breakdown line — matches spot dashboard style
            st.caption(
                f"Clusters: {s['n_clusters']} independent sessions + "
                f"{s['n_singles']} single trades  |  "
                f"Avg win: {s['avg_win_pct']:+.2f}%  |  "
                f"Avg loss: {s['avg_loss_pct']:+.2f}%"
            )
            if s["effective_n"] < s["n"]:
                st.caption(
                    f"ℹ️  raw count {s['n']} overstates independence — "
                    f"{s['n'] - s['effective_n']} trade(s) co-moved in shared clusters"
                )


# ---------------------------------------------------------------------------
# OPEN POSITIONS — helpers
# ---------------------------------------------------------------------------

def _fmt_price(val) -> str:
    """Format a price value for display — handles None and low-price assets gracefully.

    Uses dynamic decimal precision matching chart_analyzer._fmt_price so that
    assets like PEPEUSDT (0.0000034...) display meaningfully instead of
    collapsing to 0.000003 at 6dp.
    """
    if val is None:
        return "n/a"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "n/a"
    if v == 0:
        return "0"
    import math
    abs_v = abs(v)
    if abs_v >= 1000:
        decimals = 2
    elif abs_v >= 1:
        decimals = 4
    elif abs_v >= 0.1:
        decimals = 5
    elif abs_v >= 0.01:
        decimals = 6
    elif abs_v >= 0.001:
        decimals = 6
    elif abs_v >= 1e-4:
        decimals = 8
    elif abs_v >= 1e-5:
        decimals = 9
    elif abs_v >= 1e-6:
        decimals = 10
    else:
        # enough sig figs for anything smaller
        decimals = max(10, -int(math.floor(math.log10(abs_v))) + 3)
    if abs_v >= 1000:
        return f"{v:,.{decimals}f}"
    return f"{v:.{decimals}f}"


def _pct_color(pct: float | None) -> str:
    """Return green/red CSS colour string based on sign."""
    if pct is None:
        return "gray"
    return "#2ca02c" if pct >= 0 else "#d62728"


def _req_get(url: str, params: dict | None = None, timeout: int = 8):
    """
    Wrapper around requests.get that:
    1. Always sends a browser-like User-Agent (Binance is behind Cloudflare
       which blocks requests without one).
    2. Falls back to verify=False if SSL certificate verification fails —
       common with VPN/proxy setups that intercept TLS.
    Both are safe for public read-only price endpoints.
    """
    import requests as _req
    import warnings

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        return _req.get(url, params=params, timeout=timeout, headers=headers)
    except Exception as e:
        if "SSL" in str(e) or "certificate" in str(e).lower() or "CERTIFICATE" in str(e):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return _req.get(url, params=params, timeout=timeout,
                                headers=headers, verify=False)
        raise


def _fetch_spot_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch current prices for spot positions.

    Source priority:
    1. Binance Mainnet via python-binance (no-auth, just tickers) — real prices
    2. Binance Spot Testnet — fallback for symbols not on mainnet
    3. Raw public mainnet HTTPS — last resort (may fail on SSL-restricted networks)
    """
    prices: dict[str, float] = {}
    if not symbols:
        return prices

    sym_set = set(symbols)

    # ── Primary: Binance Mainnet public tickers (python-binance, no auth) ──
    # Uses the library's session which handles SSL correctly, unlike raw requests
    # on some networks. No API key needed for ticker data.
    try:
        from binance.client import Client as _Client
        _mainnet = _Client("", "")  # no auth needed for public ticker endpoint
        tickers = _mainnet.get_all_tickers()
        for item in tickers:
            if item.get("symbol") in sym_set:
                prices[item["symbol"]] = float(item["price"])
    except Exception:
        pass

    # ── Fallback: Binance Spot Testnet (for symbols genuinely not on mainnet) ──
    # Note: testnet prices are often stale/frozen for low-liquidity symbols.
    # Only use for symbols we couldn't get from mainnet.
    missing = sym_set - prices.keys()
    if missing:
        try:
            api_key    = os.getenv("BINANCE_TESTNET_API_KEY", "")
            api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
            if api_key and api_secret:
                from binance.client import Client as _Client
                _c = _Client(api_key, api_secret, testnet=True)
                tickers = _c.get_all_tickers()
                for item in tickers:
                    if item.get("symbol") in missing:
                        prices[item["symbol"]] = float(item["price"])
        except Exception:
            pass

    # ── Last resort: raw public mainnet HTTPS ──────────────────────────
    for sym in sym_set - prices.keys():
        try:
            resp = _req_get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": sym},
                timeout=6,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "price" in data:
                    prices[sym] = float(data["price"])
        except Exception:
            pass

    return prices


def _fetch_futures_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch current prices using Binance Futures Testnet (same client as
    futures_trade_executor.py). Falls back to spot testnet prices (which
    are close enough for display) if futures testnet keys not configured.
    """
    prices: dict[str, float] = {}
    if not symbols:
        return prices

    sym_set = set(symbols)

    # ── Primary: Binance Futures Testnet ───────────────────────────────
    try:
        api_key    = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY", "")
        api_secret = os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET", "")
        if api_key and api_secret:
            from binance.client import Client as _Client
            import requests as _rq
            _c = _Client(api_key, api_secret, testnet=True)
            _c.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
            tickers = _c.futures_symbol_ticker()
            for item in (tickers if isinstance(tickers, list) else [tickers]):
                if item.get("symbol") in sym_set:
                    prices[item["symbol"]] = float(item["price"])
    except Exception:
        pass

    # ── Fallback: spot testnet (prices close enough for display) ────────
    missing = sym_set - prices.keys()
    if missing:
        spot = _fetch_spot_prices(list(missing))
        prices.update(spot)

    return prices


def _status_badge(status: str) -> str:
    """Return an emoji badge for entry/exit status."""
    return {
        "NEW":              "🕐 NEW",
        "PARTIALLY_FILLED": "🔄 PARTIAL",
        "FILLED":           "✅ FILLED",
        "CANCELED":         "⚪️ CANCELED",
        "OPEN":             "🟢 OPEN",
        "TP_HIT":           "🟢 TP HIT",
        "SL_HIT":           "🔴 SL HIT",
    }.get(str(status).upper(), str(status))


def render_spot_open_card(trade: dict, current_price: float | None) -> None:
    """
    Render a single open spot position as a visual card.
    All original data preserved — layout redesigned for readability.
    """
    sym          = trade.get("symbol", "?")
    direction    = str(trade.get("direction", "long")).upper()
    entry_status = str(trade.get("entry_status", "NEW")).upper()
    entry_price  = trade.get("entry_price")
    fill_price   = trade.get("entry_fill_price")
    sl           = trade.get("sl")
    tp1          = trade.get("tp1")
    qty          = trade.get("entry_qty") or 0
    oco_placed   = trade.get("oco_placed", False)
    oco_list_id  = trade.get("oco_list_id")
    rr           = trade.get("planned_rr")
    risk_pct     = trade.get("risk_pct")
    cluster_id   = trade.get("correlation_cluster_id")
    open_time    = trade.get("open_time", "")
    slip_pct     = trade.get("slippage_pct")
    notional     = trade.get("entry_notional")

    ref_price = fill_price or entry_price

    # ── Derived ───────────────────────────────────────────────────────
    unreal_pnl:   float | None = None
    pct_to_fill:  float | None = None
    pct_to_sl:    float | None = None
    pct_to_tp:    float | None = None

    if current_price and current_price > 0:
        if entry_status == "FILLED" and ref_price and qty:
            unreal_pnl = float(qty) * (current_price - float(ref_price))
        if entry_status in ("NEW", "PARTIALLY_FILLED") and entry_price:
            pct_to_fill = (float(entry_price) - current_price) / current_price * 100
        if sl and entry_price:
            # % from entry — always meaningful regardless of testnet price discrepancy
            pct_to_sl = (float(sl) - float(entry_price)) / float(entry_price) * 100
        if tp1 and entry_price:
            pct_to_tp = (float(tp1) - float(entry_price)) / float(entry_price) * 100

    ot_str = ""
    if open_time:
        try:
            ot_str = str(open_time)[:19].replace("T", " ") + " UTC"
        except Exception:
            ot_str = str(open_time)

    is_filled    = entry_status == "FILLED"
    is_pending   = entry_status in ("NEW", "PARTIALLY_FILLED")
    status_color = {"FILLED": "#2ca02c", "NEW": "#ff7f0e", "PARTIALLY_FILLED": "#1f77b4"}.get(entry_status, "#888")
    status_icon  = {"FILLED": "✅", "NEW": "🕐", "PARTIALLY_FILLED": "🔄"}.get(entry_status, "❓")
    oco_ok       = oco_placed and oco_list_id
    oco_badge    = (f"<span style='background:#1a7a1a;color:#fff;border-radius:4px;"
                    f"padding:1px 7px;font-size:0.78em'>OCO ✓</span>"
                    if oco_ok else
                    (f"<span style='background:#7a1a1a;color:#fff;border-radius:4px;"
                     f"padding:1px 7px;font-size:0.78em'>⚠ NO OCO</span>"
                     if is_filled else ""))

    # PENDING FILL badge — shown when order is NEW or PARTIALLY_FILLED
    pending_badge = (
        f"<span style='background:#b85c00;color:#fff;border-radius:4px;"
        f"padding:2px 8px;font-size:0.78em;font-weight:700;letter-spacing:0.04em'>"
        f"⏳ PENDING FILL</span>"
        if entry_status == "NEW" else
        f"<span style='background:#1f5fa6;color:#fff;border-radius:4px;"
        f"padding:2px 8px;font-size:0.78em;font-weight:700;letter-spacing:0.04em'>"
        f"🔄 PARTIAL FILL</span>"
        if entry_status == "PARTIALLY_FILLED" else ""
    )

    pnl_color    = "#2ca02c" if (unreal_pnl or 0) >= 0 else "#d62728"

    with st.container(border=True):
        # ── Header row ────────────────────────────────────────────────
        h1, h2 = st.columns([3, 2])
        with h1:
            st.markdown(
                f"<div style='line-height:1.3'>"
                f"<span style='font-size:1.25em;font-weight:700'>{sym}</span>"
                f"&nbsp;&nbsp;"
                f"<code style='background:#e8f4e8;color:#1a6b1a;padding:2px 8px;"
                f"border-radius:4px;font-size:0.9em'>LONG</code>"
                f"&nbsp;&nbsp;{oco_badge}"
                f"{'&nbsp;&nbsp;' + pending_badge if pending_badge else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with h2:
            st.markdown(
                f"<div style='text-align:right;line-height:1.3'>"
                f"<span style='font-size:0.8em;color:#aaa'>{ot_str}</span><br>"
                f"{'<span style=\"font-size:0.8em;color:#888\">cluster: <code>' + cluster_id + '</code></span>' if cluster_id else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # ── Status + PnL banner ───────────────────────────────────────
        if is_filled and unreal_pnl is not None:
            pnl_bg = "rgba(44,160,44,0.08)" if unreal_pnl >= 0 else "rgba(214,39,40,0.08)"
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"background:{pnl_bg};border-radius:6px;padding:6px 12px;margin-bottom:8px'>"
                f"<span style='color:{status_color};font-weight:600'>{status_icon} {entry_status}</span>"
                f"<span style='font-size:1.1em;font-weight:700;color:{pnl_color}'>"
                f"Unrealized&nbsp;{unreal_pnl:+.4f} USDT</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        elif is_pending:
            # Distinct amber banner for orders awaiting fill
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"background:rgba(184,92,0,0.10);border-left:4px solid #b85c00;"
                f"border-radius:0 6px 6px 0;padding:7px 12px;margin-bottom:8px'>"
                f"<span style='color:{status_color};font-weight:700'>{status_icon} {entry_status}</span>"
                f"<span style='font-size:0.88em;font-weight:700;color:#b85c00'>"
                f"⏳ Waiting for exchange fill…</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div style='background:rgba(255,127,14,0.12);border-left:3px solid #ff7f0e;"
                f"border-radius:0 6px 6px 0;padding:6px 12px;margin-bottom:8px'>"
                f"<span style='color:{status_color};font-weight:600'>{status_icon} {entry_status}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # ── Price grid ────────────────────────────────────────────────
        pc1, pc2, pc3 = st.columns(3)
        with pc1:
            st.markdown(
                f"<div style='text-align:center;padding:4px'>"
                f"<div style='font-size:0.72em;color:#888;text-transform:uppercase;letter-spacing:0.05em'>Entry</div>"
                f"<div style='font-size:0.98em;font-weight:600;font-family:monospace'>{_fmt_price(entry_price)}</div>"
                f"{'<div style=\"font-size:0.78em;color:#aaa\">fill ' + _fmt_price(fill_price) + '</div>' if is_filled and fill_price and fill_price != entry_price else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with pc2:
            cur_str = _fmt_price(current_price) if current_price else "—"
            dist_str = f"{pct_to_fill:+.2f}%" if pct_to_fill is not None else ""
            dist_color = "#2ca02c" if (pct_to_fill or 0) <= 0 else "#ff7f0e"
            st.markdown(
                f"<div style='text-align:center;padding:4px;border-radius:6px;"
                f"border:1px solid rgba(128,128,128,0.25)'>"
                f"<div style='font-size:0.72em;color:#888;text-transform:uppercase;"
                f"letter-spacing:0.05em'>Current</div>"
                f"<div style='font-size:1.05em;font-weight:700;font-family:monospace'>{cur_str}</div>"
                f"{'<div style=\"font-size:0.78em;color:' + dist_color + '\">' + dist_str + ' to fill</div>' if dist_str else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with pc3:
            notional_str = f"${float(notional):.2f}" if notional else "—"
            qty_str      = f"{float(qty):.4f}" if qty else "—"
            st.markdown(
                f"<div style='text-align:center;padding:4px'>"
                f"<div style='font-size:0.72em;color:#888;text-transform:uppercase;letter-spacing:0.05em'>Size</div>"
                f"<div style='font-size:0.98em;font-weight:600;font-family:monospace'>{qty_str}</div>"
                f"<div style='font-size:0.78em;color:#aaa'>{notional_str} notional</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # ── SL / TP row ───────────────────────────────────────────────
        sc1, sc2, sc3 = st.columns([2, 2, 1])
        with sc1:
            sl_pct = f"  {pct_to_sl:+.2f}%" if pct_to_sl is not None else ""
            st.markdown(
                f"<div style='background:rgba(214,39,40,0.07);border-left:3px solid #d62728;"
                f"border-radius:0 6px 6px 0;padding:6px 10px'>"
                f"<div style='font-size:0.72em;color:#d62728;font-weight:600'>STOP LOSS</div>"
                f"<div style='font-family:monospace;font-weight:700'>{_fmt_price(sl)}"
                f"<span style='font-size:0.8em;color:#888;font-weight:400'>{sl_pct}</span></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with sc2:
            tp_pct = f"  {pct_to_tp:+.2f}%" if pct_to_tp is not None else ""
            st.markdown(
                f"<div style='background:rgba(44,160,44,0.07);border-left:3px solid #2ca02c;"
                f"border-radius:0 6px 6px 0;padding:6px 10px'>"
                f"<div style='font-size:0.72em;color:#2ca02c;font-weight:600'>TAKE PROFIT</div>"
                f"<div style='font-family:monospace;font-weight:700'>{_fmt_price(tp1)}"
                f"<span style='font-size:0.8em;color:#888;font-weight:400'>{tp_pct}</span></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with sc3:
            rr_val   = f"{float(rr):.2f}:1"   if rr       else "—"
            risk_val = f"{float(risk_pct):.2f}%" if risk_pct else "—"
            st.markdown(
                f"<div style='text-align:center;padding:4px'>"
                f"<div style='font-size:0.72em;color:#888'>R:R</div>"
                f"<div style='font-weight:700'>{rr_val}</div>"
                f"<div style='font-size:0.78em;color:#888'>risk {risk_val}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # ── Footer ────────────────────────────────────────────────────
        foot_parts = []
        if slip_pct is not None:
            foot_parts.append(f"Slip: {slip_pct:+.3f}%")
        if oco_list_id:
            foot_parts.append(f"OCO #{oco_list_id}")
        if foot_parts:
            st.markdown(
                f"<div style='font-size:0.78em;color:#aaa;margin-top:4px'>"
                f"{'  ·  '.join(foot_parts)}"
                f"</div>",
                unsafe_allow_html=True,
            )


def render_futures_open_card(trade: dict, current_price: float | None) -> None:
    """
    Render a single open futures position as a visual card.
    All original data preserved — layout redesigned for readability.
    """
    sym          = trade.get("symbol", "?")
    side         = str(trade.get("position_side", "LONG")).upper()
    entry_status = str(trade.get("entry_status", "NEW")).upper()
    entry_price  = trade.get("entry_price")
    fill_price   = trade.get("entry_fill_price")
    sl           = trade.get("sl")
    tp1          = trade.get("tp1")
    qty          = trade.get("entry_qty") or 0
    liq_price    = trade.get("liquidation_price")
    liq_dist     = trade.get("distance_to_liquidation_pct")
    leverage     = trade.get("leverage")
    margin_mode  = trade.get("margin_mode", "isolated")
    funding_paid = trade.get("funding_rate_paid") or 0.0
    vol_regime   = trade.get("volatility_regime_at_entry", "unknown")
    rr           = trade.get("planned_rr")
    risk_pct     = trade.get("risk_pct")
    cluster_id   = trade.get("correlation_cluster_id")
    open_time    = trade.get("open_time", "")
    slip_pct     = trade.get("slippage_pct")
    notional     = trade.get("entry_notional")
    margin_used  = trade.get("margin_used")

    ref_price = fill_price or entry_price

    # ── Derived ───────────────────────────────────────────────────────
    unreal_pnl:  float | None = None
    pct_to_fill: float | None = None
    pct_to_sl:   float | None = None
    pct_to_tp:   float | None = None
    pct_to_liq:  float | None = None

    if current_price and current_price > 0:
        mult = 1 if side == "LONG" else -1
        if entry_status == "FILLED" and ref_price and qty:
            unreal_pnl = float(qty) * (current_price - float(ref_price)) * mult
        if entry_status in ("NEW", "PARTIALLY_FILLED") and entry_price:
            pct_to_fill = (float(entry_price) - current_price) / current_price * 100
        if sl and entry_price:
            pct_to_sl = (float(sl) - float(entry_price)) / float(entry_price) * 100
        if tp1 and entry_price:
            pct_to_tp = (float(tp1) - float(entry_price)) / float(entry_price) * 100
        if liq_price:
            pct_to_liq = (float(liq_price) - current_price) / current_price * 100

    ot_str = ""
    if open_time:
        try:
            ot_str = str(open_time)[:19].replace("T", " ") + " UTC"
        except Exception:
            ot_str = str(open_time)

    is_filled    = entry_status == "FILLED"
    is_pending   = entry_status in ("NEW", "PARTIALLY_FILLED")
    is_long      = side == "LONG"
    side_color   = "#1f77b4" if is_long else "#d62728"
    side_bg      = "rgba(31,119,180,0.10)" if is_long else "rgba(214,39,40,0.10)"
    side_icon    = "📈" if is_long else "📉"
    status_color = {"FILLED": "#2ca02c", "NEW": "#ff7f0e", "PARTIALLY_FILLED": "#1f77b4"}.get(entry_status, "#888")
    status_icon  = {"FILLED": "✅", "NEW": "🕐", "PARTIALLY_FILLED": "🔄"}.get(entry_status, "❓")
    pnl_color    = "#2ca02c" if (unreal_pnl or 0) >= 0 else "#d62728"
    lev_str      = f"{int(float(leverage))}x" if leverage else "?"
    regime_icon  = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(str(vol_regime).lower(), "⚪")
    liq_dist_val = float(liq_dist) if liq_dist is not None else None
    liq_warn     = liq_dist_val is not None and liq_dist_val < 10.0

    # PENDING FILL badge — shown when futures order not yet filled
    pending_badge = (
        f"<span style='background:#b85c00;color:#fff;border-radius:4px;"
        f"padding:2px 8px;font-size:0.78em;font-weight:700;letter-spacing:0.04em'>"
        f"⏳ PENDING FILL</span>"
        if entry_status == "NEW" else
        f"<span style='background:#1f5fa6;color:#fff;border-radius:4px;"
        f"padding:2px 8px;font-size:0.78em;font-weight:700;letter-spacing:0.04em'>"
        f"🔄 PARTIAL FILL</span>"
        if entry_status == "PARTIALLY_FILLED" else ""
    )

    with st.container(border=True):
        # ── Header row ────────────────────────────────────────────────
        h1, h2 = st.columns([3, 2])
        with h1:
            st.markdown(
                f"<div style='line-height:1.4'>"
                f"<span style='font-size:1.25em;font-weight:700'>{sym}</span>"
                f"&nbsp;&nbsp;"
                f"<code style='background:{side_bg};color:{side_color};padding:2px 9px;"
                f"border-radius:4px;font-size:0.9em'>{side_icon} {side}</code>"
                f"&nbsp;&nbsp;"
                f"<span style='background:#f0f0f0;border-radius:4px;padding:2px 8px;"
                f"font-size:0.82em;color:#444'>{lev_str} {margin_mode}</span>"
                f"{'&nbsp;&nbsp;' + pending_badge if pending_badge else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with h2:
            st.markdown(
                f"<div style='text-align:right;line-height:1.4'>"
                f"<span style='font-size:0.79em;color:#aaa'>{ot_str}</span><br>"
                f"{'<span style=\"font-size:0.79em;color:#888\">cluster: <code>' + cluster_id + '</code></span>' if cluster_id else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:5px'></div>", unsafe_allow_html=True)

        # ── PnL / status banner ───────────────────────────────────────
        if is_filled and unreal_pnl is not None:
            pnl_bg = "rgba(44,160,44,0.08)" if unreal_pnl >= 0 else "rgba(214,39,40,0.08)"
            fund_str = (f"&nbsp;&nbsp;·&nbsp;&nbsp;"
                        f"<span style='color:{'#d62728' if funding_paid > 0 else '#2ca02c'}'>"
                        f"Funding {funding_paid:+.4f}</span>"
                        if funding_paid != 0.0 else "")
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"background:{pnl_bg};border-radius:6px;padding:6px 12px;margin-bottom:8px'>"
                f"<span style='color:{status_color};font-weight:600'>{status_icon} {entry_status}"
                f"{fund_str}</span>"
                f"<span style='font-size:1.1em;font-weight:700;color:{pnl_color}'>"
                f"Unrealized&nbsp;{unreal_pnl:+.4f} USDT</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        elif is_pending:
            # Distinct amber banner for orders awaiting fill
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"background:rgba(184,92,0,0.10);border-left:4px solid #b85c00;"
                f"border-radius:0 6px 6px 0;padding:7px 12px;margin-bottom:8px'>"
                f"<span style='color:{status_color};font-weight:700'>{status_icon} {entry_status}</span>"
                f"<span style='font-size:0.88em;font-weight:700;color:#b85c00'>"
                f"⏳ Waiting for exchange fill…</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div style='background:rgba(255,127,14,0.12);border-left:3px solid #ff7f0e;"
                f"border-radius:0 6px 6px 0;padding:6px 12px;margin-bottom:8px'>"
                f"<span style='color:{status_color};font-weight:600'>{status_icon} {entry_status}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # ── Price grid: Entry | Current | Size ────────────────────────
        pc1, pc2, pc3 = st.columns(3)
        with pc1:
            st.markdown(
                f"<div style='text-align:center;padding:4px'>"
                f"<div style='font-size:0.72em;color:#888;text-transform:uppercase;letter-spacing:0.05em'>Entry</div>"
                f"<div style='font-size:0.98em;font-weight:600;font-family:monospace'>{_fmt_price(entry_price)}</div>"
                f"{'<div style=\"font-size:0.78em;color:#aaa\">fill ' + _fmt_price(fill_price) + '</div>' if is_filled and fill_price and fill_price != entry_price else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with pc2:
            cur_str  = _fmt_price(current_price) if current_price else "—"
            if pct_to_fill is not None:
                dist_color = "#ff7f0e"
                dist_str   = f"{pct_to_fill:+.2f}% to fill"
            else:
                dist_str   = ""
                dist_color = "#888"
            st.markdown(
                f"<div style='text-align:center;padding:4px;border-radius:6px;"
                f"border:1px solid rgba(128,128,128,0.25)'>"
                f"<div style='font-size:0.72em;color:#888;text-transform:uppercase;letter-spacing:0.05em'>Current</div>"
                f"<div style='font-size:1.05em;font-weight:700;font-family:monospace'>{cur_str}</div>"
                f"{'<div style=\"font-size:0.78em;color:' + dist_color + '\">' + dist_str + '</div>' if dist_str else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with pc3:
            notional_str  = f"${float(notional):.2f}" if notional else "—"
            margin_str    = f"${float(margin_used):.2f}" if margin_used else "—"
            qty_str       = f"{float(qty):.4f}" if qty else "—"
            st.markdown(
                f"<div style='text-align:center;padding:4px'>"
                f"<div style='font-size:0.72em;color:#888;text-transform:uppercase;letter-spacing:0.05em'>Size</div>"
                f"<div style='font-size:0.98em;font-weight:600;font-family:monospace'>{qty_str}</div>"
                f"<div style='font-size:0.78em;color:#aaa'>{notional_str} · margin {margin_str}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:5px'></div>", unsafe_allow_html=True)

        # ── SL / TP / R:R row ─────────────────────────────────────────
        sc1, sc2, sc3 = st.columns([2, 2, 1])
        with sc1:
            sl_pct = f"  {pct_to_sl:+.2f}%" if pct_to_sl is not None else ""
            st.markdown(
                f"<div style='background:rgba(214,39,40,0.07);border-left:3px solid #d62728;"
                f"border-radius:0 6px 6px 0;padding:6px 10px'>"
                f"<div style='font-size:0.72em;color:#d62728;font-weight:600'>STOP LOSS</div>"
                f"<div style='font-family:monospace;font-weight:700'>{_fmt_price(sl)}"
                f"<span style='font-size:0.8em;color:#888;font-weight:400'>{sl_pct}</span></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with sc2:
            tp_pct = f"  {pct_to_tp:+.2f}%" if pct_to_tp is not None else ""
            st.markdown(
                f"<div style='background:rgba(44,160,44,0.07);border-left:3px solid #2ca02c;"
                f"border-radius:0 6px 6px 0;padding:6px 10px'>"
                f"<div style='font-size:0.72em;color:#2ca02c;font-weight:600'>TAKE PROFIT</div>"
                f"<div style='font-family:monospace;font-weight:700'>{_fmt_price(tp1)}"
                f"<span style='font-size:0.8em;color:#888;font-weight:400'>{tp_pct}</span></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with sc3:
            rr_val   = f"{float(rr):.2f}:1"    if rr       else "—"
            risk_val = f"{float(risk_pct):.2f}%" if risk_pct else "—"
            st.markdown(
                f"<div style='text-align:center;padding:4px'>"
                f"<div style='font-size:0.72em;color:#888'>R:R</div>"
                f"<div style='font-weight:700'>{rr_val}</div>"
                f"<div style='font-size:0.78em;color:#888'>risk {risk_val}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:5px'></div>", unsafe_allow_html=True)

        # ── Liquidation row ───────────────────────────────────────────
        lc1, lc2 = st.columns([3, 1])
        with lc1:
            liq_color    = "#d62728" if liq_warn else "#888"
            liq_dist_str = f"{liq_dist_val:.2f}% away" if liq_dist_val is not None else "n/a"
            liq_pct_str  = f"  ({pct_to_liq:+.2f}% from current)" if pct_to_liq is not None else ""
            warn_badge   = ("&nbsp;<span style='background:#d62728;color:#fff;border-radius:3px;"
                            "padding:1px 5px;font-size:0.72em'>⚠ TIGHT</span>"
                            if liq_warn else "")
            st.markdown(
                f"<div style='font-size:0.85em;font-family:monospace'>"
                f"<span style='color:#888'>Liq:</span>&nbsp;"
                f"<b>{_fmt_price(liq_price)}</b>&nbsp;"
                f"<span style='color:{liq_color}'>{liq_dist_str}</span>"
                f"<span style='color:#aaa'>{liq_pct_str}</span>"
                f"{warn_badge}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with lc2:
            st.markdown(
                f"<div style='text-align:right;font-size:0.85em'>"
                f"{regime_icon}&nbsp;<span style='color:#888'>{vol_regime}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # ── Footer ────────────────────────────────────────────────────
        foot_parts = []
        if slip_pct is not None:
            foot_parts.append(f"Slip: {slip_pct:+.3f}%")
        if foot_parts:
            st.markdown(
                f"<div style='font-size:0.78em;color:#aaa;margin-top:4px'>"
                f"{'  ·  '.join(foot_parts)}"
                f"</div>",
                unsafe_allow_html=True,
            )


def render_resolved_card(trade: dict, trade_type: str = "spot") -> None:
    """
    Render a recently-resolved trade (TP_HIT / SL_HIT) as a compact card.
    PnL is prominently coloured green/red.
    """
    sym         = trade.get("symbol", "?")
    exit_status = str(trade.get("exit_status", "")).upper()
    pnl_usd     = trade.get("realized_pnl_usd")
    pnl_pct     = trade.get("realized_pnl_pct")
    entry_price = trade.get("entry_price")
    exit_price  = trade.get("exit_price")
    planned_rr  = trade.get("planned_rr")
    cluster_id  = trade.get("correlation_cluster_id")
    direction   = trade.get("direction") or trade.get("position_side") or "?"

    is_win      = exit_status == "TP_HIT"
    is_canceled = exit_status == "CANCELED"
    pnl_color   = "#808080" if is_canceled else ("#2ca02c" if is_win else "#d62728")
    border_css  = f"border-left: 4px solid {pnl_color}; padding-left: 10px;"
    icon        = "⚪️" if is_canceled else ("🟢" if is_win else "🔴")
    label       = "CANCELED" if is_canceled else ("TP HIT" if is_win else "SL HIT")

    # Exit time
    exit_time_raw = trade.get("exit_time")
    if exit_time_raw is None and is_canceled:
        ot = trade.get("open_time")
        if ot:
            try:
                exit_time_raw = pd.to_datetime(ot).timestamp() * 1000
            except:
                pass

    exit_time_str = "n/a"
    if exit_time_raw is not None:
        try:
            et = pd.to_datetime(float(exit_time_raw), unit="ms", utc=True)
            exit_time_str = et.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            exit_time_str = str(exit_time_raw)[:19]

    # Time in position
    tip = trade.get("time_to_resolution_sec") or trade.get("time_in_position_sec")
    if tip:
        h, rem = divmod(int(tip), 3600)
        m_val  = rem // 60
        tip_str = f"{h}h {m_val}m" if h else f"{m_val}m"
    else:
        tip_str = "n/a"

    # Futures-specific extras
    extras = ""
    if trade_type == "futures":
        side     = str(trade.get("position_side", "?")).upper()
        funding  = trade.get("funding_rate_paid") or 0.0
        regime   = trade.get("volatility_regime_at_entry", "?")
        side_icon = "📈" if side == "LONG" else "📉"
        extras = (
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{side_icon} {side}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;Funding: ${float(funding):+.4f}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;Regime: {regime}"
        )

    with st.container(border=True):
        st.markdown(
            f"<div style='{border_css}'>"
            f"<span style='font-size:1.1em;font-weight:bold'>{icon}&nbsp;{sym}</span>"
            f"&nbsp;&nbsp;<code>{label}</code>"
            f"{'&nbsp;&nbsp;<span style=\"font-size:0.85em;color:#888\">' + trade_type.upper() + '</span>' if trade_type == 'futures' else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )

        # PnL — big and coloured
        pnl_usd_str = f"<span style='color:{pnl_color};font-size:1.5em;font-weight:bold'>${float(pnl_usd):+.4f}</span>" \
                      if pnl_usd is not None else "<span style='color:#888'>n/a</span>"
        pnl_pct_str = f"<span style='color:{pnl_color}'>&nbsp;({float(pnl_pct):+.2f}%)</span>" \
                      if pnl_pct is not None else ""

        st.markdown(
            f"<div style='font-family:monospace;font-size:0.95em;line-height:2.2;margin-top:4px'>"
            f"{pnl_usd_str}{pnl_pct_str}"
            f"&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Entry:&nbsp;<b>{_fmt_price(entry_price)}</b>"
            f"&nbsp;&nbsp;→&nbsp;&nbsp;"
            f"Exit:&nbsp;<b>{_fmt_price(exit_price)}</b>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"R:R&nbsp;{float(planned_rr):.2f}:1" if planned_rr else
            f"{pnl_usd_str}{pnl_pct_str}"
            f"&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Entry:&nbsp;<b>{_fmt_price(entry_price)}</b>"
            f"&nbsp;&nbsp;→&nbsp;&nbsp;"
            f"Exit:&nbsp;<b>{_fmt_price(exit_price)}</b>",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"<div style='font-size:0.85em;color:#888;margin-top:2px'>"
            f"Closed: {exit_time_str}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;held {tip_str}"
            f"{extras}"
            f"{'&nbsp;&nbsp;·&nbsp;&nbsp;cluster: <code>' + cluster_id + '</code>' if cluster_id else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )


def render_open_positions_tab(
    spot_rows: list[dict],
    futures_rows: list[dict],
) -> None:
    """
    Render the full "Open Positions" tab:
    - Spot sub-tab: cards grouped by cluster_id
    - Futures sub-tab: cards grouped by cluster_id
    - Recently Resolved section (last 24h) at the bottom of each sub-tab
    """
    from datetime import datetime, timezone, timedelta

    now_utc   = datetime.now(timezone.utc)
    cutoff_ms = (now_utc - timedelta(hours=24)).timestamp() * 1000

    # ── Split open vs recently resolved ───────────────────────────────
    spot_open     = [t for t in spot_rows     if t.get("exit_status") == "OPEN"]
    futures_open  = [t for t in futures_rows  if t.get("exit_status") == "OPEN"]

    def _is_recent(t: dict) -> bool:
        et = t.get("exit_time")
        if et is None:
            if t.get("exit_status") == "CANCELED":
                ot = t.get("open_time")
                if ot:
                    try:
                        return pd.to_datetime(ot).timestamp() * 1000 >= cutoff_ms
                    except Exception:
                        pass
            return False
        try:
            return float(et) >= cutoff_ms
        except (TypeError, ValueError):
            return False

    def _sort_key(t: dict) -> float:
        et = t.get("exit_time")
        if et is not None:
            try: return float(et)
            except: pass
        if t.get("exit_status") == "CANCELED":
            ot = t.get("open_time")
            if ot:
                try: return pd.to_datetime(ot).timestamp() * 1000
                except: pass
        return 0.0

    spot_resolved    = [t for t in spot_rows
                        if t.get("exit_status") in ("TP_HIT", "SL_HIT", "CANCELED") and _is_recent(t)]
    futures_resolved = [t for t in futures_rows
                        if t.get("exit_status") in ("TP_HIT", "SL_HIT", "CANCELED") and _is_recent(t)]

    # Sort resolved by exit_time desc (most recent first)
    spot_resolved.sort(key=_sort_key, reverse=True)
    futures_resolved.sort(key=_sort_key, reverse=True)

    # ── Fetch live prices for open positions ──────────────────────────
    spot_syms    = list({t["symbol"] for t in spot_open    if t.get("symbol")})
    futures_syms = list({t["symbol"] for t in futures_open if t.get("symbol")})

    with st.spinner("Fetching live prices..."):
        try:
            spot_prices = _fetch_spot_prices(spot_syms)
        except Exception as e:
            st.error("Failed to fetch live spot prices.")
            st.exception(e)
            spot_prices = {}
            
        try:
            futures_prices = _fetch_futures_prices(futures_syms)
        except Exception as e:
            st.error("Failed to fetch live futures prices.")
            st.exception(e)
            futures_prices = {}

    # ── Sub-tabs ──────────────────────────────────────────────────────
    sub_spot, sub_futures = st.tabs([
        f"📈 Spot ({len(spot_open)} open)",
        f"⚡ Futures ({len(futures_open)} open)",
    ])

    # ════════════════ SPOT SUB-TAB ════════════════════════════════════
    with sub_spot:
        st.caption("Live data from Supabase  |  Prices from Binance public API  |  Read-only")

        if not spot_open and not spot_resolved:
            st.info("No open or recently-resolved spot positions.")
        else:
            if spot_open:
                # Group by cluster_id
                from collections import defaultdict as _dd
                clusters: dict = _dd(list)
                for t in spot_open:
                    cid = t.get("correlation_cluster_id") or "single"
                    clusters[cid].append(t)

                for cid, group in clusters.items():
                    if cid == "single":
                        st.subheader(f"Single trades ({len(group)})")
                    else:
                        st.subheader(f"Cluster `{cid}`  —  {len(group)} position(s)")

                    for trade in group:
                        sym   = trade.get("symbol", "")
                        price = spot_prices.get(sym)
                        render_spot_open_card(trade, price)
                        st.write("")  # small spacer
            else:
                st.info("No open spot positions.")

            # ── Recently Resolved (24h) ───────────────────────────────
            st.divider()
            st.subheader(f"Recently Resolved — last 24h ({len(spot_resolved)} trade(s))")
            if spot_resolved:
                for trade in spot_resolved:
                    render_resolved_card(trade, trade_type="spot")
                    st.write("")
            else:
                st.info("No spot trades resolved in the last 24 hours.")

    # ════════════════ FUTURES SUB-TAB ═════════════════════════════════
    with sub_futures:
        st.caption("Live data from Supabase  |  Mark prices from Binance Futures API  |  Read-only")

        if not futures_open and not futures_resolved:
            st.info("No open or recently-resolved futures positions.")
        else:
            if futures_open:
                from collections import defaultdict as _dd2
                fclusters: dict = _dd2(list)
                for t in futures_open:
                    cid = t.get("correlation_cluster_id") or "single"
                    fclusters[cid].append(t)

                for cid, group in fclusters.items():
                    if cid == "single":
                        st.subheader(f"Single trades ({len(group)})")
                    else:
                        st.subheader(f"Cluster `{cid}`  —  {len(group)} position(s)")

                    for trade in group:
                        sym   = trade.get("symbol", "")
                        price = futures_prices.get(sym)
                        render_futures_open_card(trade, price)
                        st.write("")
            else:
                st.info("No open futures positions.")

            # ── Recently Resolved (24h) ───────────────────────────────
            st.divider()
            st.subheader(f"Recently Resolved — last 24h ({len(futures_resolved)} trade(s))")
            if futures_resolved:
                for trade in futures_resolved:
                    render_resolved_card(trade, trade_type="futures")
                    st.write("")
            else:
                st.info("No futures trades resolved in the last 24 hours.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    st.title("Swing Trade Dashboard")

    # ── Auto-rerun once on first load to pick up WebSocket connected state ──
    # The async thread needs a few seconds to handshake with Supabase.
    # If this is the first render and WS is not yet connected, schedule
    # one rerun after a short delay so the status bar shows green without
    # the user having to manually refresh.
    if not st.session_state.get("_ws_init_done", False):
        if not global_state.connected:
            time.sleep(3)
            st.session_state["_ws_init_done"] = True
            st.rerun()
        else:
            st.session_state["_ws_init_done"] = True

    now_wib = datetime.now(WIB)
    now_str = now_wib.strftime("%H:%M:%S")

    # Independent timer-based sync: refresh at :00:05 WIB of the next hour.
    # If the VM appears down, back off to every 5 minutes until heartbeat resumes.
    _heartbeat_for_timer = None
    try:
        from services.supabase_client import fetch_heartbeat
        _heartbeat_for_timer = fetch_heartbeat()
    except Exception:
        _heartbeat_for_timer = None

    _timer_last_seen_wib = _parse_iso_to_wib((_heartbeat_for_timer or {}).get("last_seen_at"))
    _timer_time_since_last_seen = (
        (now_wib - _timer_last_seen_wib).total_seconds()
        if _timer_last_seen_wib is not None
        else float("inf")
    )
    _timer_vm_down = _timer_time_since_last_seen > VM_STALL_THRESHOLD_SECONDS
    _autorefresh_interval_ms = _get_autorefresh_interval_ms(now_wib, _timer_vm_down)
    st_autorefresh(interval=_autorefresh_interval_ms, key="dashboard_sync_watchdog")

    c_btn, c_info = st.columns([1.5, 8.5])
    with c_btn:
        if st.button("🔄 Refresh data"):
            with global_state.lock:
                global_state.spot_rows = None
                global_state.futures_rows = None
            st.rerun()

    with c_info:
        _hb = _heartbeat_for_timer
        _last_seen_dt_wib = _parse_iso_to_wib((_hb or {}).get("last_seen_at"))
        _last_seen_wib = (
            _last_seen_dt_wib.strftime("%H:%M:%S")
            if _last_seen_dt_wib is not None
            else (global_state.last_event_time or "Waiting...")
        )
        _time_since_sec = (
            (now_wib - _last_seen_dt_wib).total_seconds()
            if _last_seen_dt_wib is not None
            else float("inf")
        )

        conn_color = "#2ca02c" if global_state.connected else "#d62728"
        conn_text = "🟢 Realtime Connected" if global_state.connected else "🔴 Disconnected"

        is_stalled = _time_since_sec > VM_STALL_THRESHOLD_SECONDS
        if is_stalled:
            if _time_since_sec is not None and not math.isnan(_time_since_sec):
                stale_mins = int(_time_since_sec // 60)
            else:
                stale_mins = 999
            vm_badge = f"🔴 VM Down (Inactive > {stale_mins} mins)"
        else:
            vm_badge = "🟢 VM Active"

        components.html(f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; 
                    display: flex; align-items: center; gap: 15px; color: #555; padding-top: 6px;">
            <div style="font-size: 14px; display: flex; align-items: center; gap: 8px;">
                <strong style="color: {conn_color};">{conn_text}</strong>
                <span style="color: #ccc;">|</span>
                <strong>{vm_badge}</strong>
                <span style="color: #ccc;">|</span>
                <strong>Last Cycle: {_last_seen_wib} WIB</strong>
            </div>
        </div>
        """, height=34)

    st.write("")

    tab_spot, tab_futures, tab_open, tab_ml = st.tabs(["📈 Spot", "⚡ Futures", "📋 Open Positions", "🧪 ML Shadow Metrics"])

    # ── TAB 1: SPOT ──────────────────────────────────────────────────────────
    with tab_spot:
        st.caption("Read-only analysis — Supabase: trades_spot  |  Stats are INDEPENDENT from Futures tab")

        df = load_trade_data()

        if df.empty:
            st.info("No spot trade data available yet.")
        else:
            metrics = build_metrics(df)
            col1, col2, col3, col4, col5, col6 = st.columns(6)
            col1.metric("Total trades",   metrics["total_trades"])
            col2.metric("Resolved",       metrics["resolved_trades"])
            col3.metric("Win rate",       f"{metrics['win_rate']:.2f}%")
            col4.metric("Realized PnL",   f"${metrics['total_realized_pnl']:+.2f}")
            col5.metric("Lab capital",    f"${metrics['lab_capital']:.2f}",
                        delta=f"vs ${STARTING_LAB_CAPITAL:.0f}")
            col6.metric("Effective N",    metrics["effective_n"],
                        delta=f"raw {metrics['raw_trade_count']}")

            st.subheader("Equity curve")
            equity_fig = build_equity_curve(df)
            if equity_fig:
                st.plotly_chart(equity_fig, use_container_width=True)
            else:
                st.info("No resolved trades for equity curve.")

            st.subheader("Win/loss by symbol")
            sym_fig = build_symbol_pnl(df)
            if sym_fig:
                st.plotly_chart(sym_fig, use_container_width=True)
            else:
                st.info("No resolved trades for symbol analysis.")

            st.subheader("Win rate by hour of day")
            win_fig, pnl_fig = build_hourly_charts(df)
            if win_fig and pnl_fig:
                c1, c2 = st.columns(2)
                with c1:
                    st.plotly_chart(win_fig, use_container_width=True)
                with c2:
                    st.plotly_chart(pnl_fig, use_container_width=True)
            else:
                st.info("No resolved trades for hourly analysis.")

            st.subheader("Planned R:R vs realized PnL")
            rr_fig = build_rr_scatter(df)
            if rr_fig:
                st.plotly_chart(rr_fig, use_container_width=True)
            else:
                st.info("No resolved trades for R:R analysis.")

            st.subheader("Zone strength analysis")
            zone_fig = build_zone_strength(df)
            if zone_fig:
                st.plotly_chart(zone_fig, use_container_width=True)
            else:
                st.info("No resolved trades for zone strength analysis.")

            st.subheader("Cluster breakdown")
            cluster_df = build_cluster_breakdown(df)
            st.dataframe(cluster_df, use_container_width=True, hide_index=True)

            st.subheader("Raw data")
            with st.expander("Show full spot trade log", expanded=False):
                st.dataframe(df, use_container_width=True, hide_index=True)

    # ── TAB 2: FUTURES ───────────────────────────────────────────────────────
    with tab_futures:
        st.caption("Read-only analysis — Supabase: trades_futures  |  Stats are INDEPENDENT from Spot tab")
        st.caption("Effective N and Z-score computed per side (LONG/SHORT) independently — "
                   "consistent with --stats-futures logic")

        fdf = load_futures_data()

        if fdf.empty:
            st.info("No futures trade data yet. Run `python3 futures_trade_executor.py --propose` "
                    "to place your first futures trade.")
        else:
            # ── Header metrics ────────────────────────────────────────────────
            f_resolved     = fdf[fdf["is_resolved"]]
            f_total        = int(len(fdf))
            f_resolved_n   = int(len(f_resolved))
            f_win_rate     = round(float(f_resolved["is_win"].mean() * 100) if f_resolved_n else 0.0, 1)
            f_total_pnl    = round(float(f_resolved["realized_pnl_usd"].sum()) if f_resolved_n else 0.0, 4)
            f_total_fund   = round(float(fdf["funding_rate_paid"].fillna(0).sum()), 4)
            f_net_pnl      = round(f_total_pnl + f_total_fund, 4)
            f_avg_liq_dist = round(float(fdf["distance_to_liquidation_pct"].mean()), 2) \
                             if "distance_to_liquidation_pct" in fdf.columns else 0.0
            f_leverage     = int(fdf["leverage"].dropna().mode()[0]) \
                             if "leverage" in fdf.columns and not fdf["leverage"].dropna().empty else "?"

            col1, col2, col3, col4, col5, col6 = st.columns(6)
            col1.metric("Total trades",       f_total)
            col2.metric("Resolved",           f_resolved_n)
            col3.metric("Win rate (overall)", f"{f_win_rate:.1f}%")
            col4.metric("Realized PnL",       f"${f_total_pnl:+.4f}")
            col5.metric("Funding accrued",    f"${f_total_fund:+.4f}",
                        delta=f"Net ${f_net_pnl:+.4f}", delta_color="inverse")
            col6.metric("Avg liq distance",   f"{f_avg_liq_dist:.1f}%",
                        delta=f"{f_leverage}x leverage")

            # ── Per-side stats (z-score + effective-n independent per side) ───
            st.subheader("Stats by side — LONG / SHORT (independent effective-n & z-score)")
            side_stats = build_futures_side_stats(fdf)
            render_futures_side_stats(side_stats)

            # ── Equity curve by side ──────────────────────────────────────────
            st.subheader("Equity curve by side")
            f_equity_fig = build_futures_equity(fdf)
            if f_equity_fig:
                st.plotly_chart(f_equity_fig, use_container_width=True)
            else:
                st.info("No resolved futures trades for equity curve.")

            # ── Win/loss by symbol + side ─────────────────────────────────────
            st.subheader("Win/loss PnL by symbol & side")
            f_sym_fig = build_futures_symbol_pnl(fdf)
            if f_sym_fig:
                st.plotly_chart(f_sym_fig, use_container_width=True)
            else:
                st.info("No resolved futures trades for symbol analysis.")

            # ── R:R scatter by side ───────────────────────────────────────────
            st.subheader("Planned R:R vs realized PnL")
            f_rr_fig = build_futures_rr_scatter(fdf)
            if f_rr_fig:
                st.plotly_chart(f_rr_fig, use_container_width=True)
            else:
                st.info("No resolved futures trades for R:R analysis.")

            # ── Futures-specific details table ────────────────────────────────
            st.subheader("Open & closed futures positions")
            display_cols = [c for c in [
                "symbol", "position_side", "entry_status", "exit_status",
                "entry_price", "entry_fill_price", "sl", "tp1",
                "leverage", "liquidation_price", "distance_to_liquidation_pct",
                "realized_pnl_usd", "realized_pnl_pct", "funding_rate_paid",
                "planned_rr", "risk_pct", "volatility_regime_at_entry",
                "correlation_cluster_id", "open_time",
            ] if c in fdf.columns]
            with st.expander("Show full futures trade log", expanded=False):
                st.dataframe(fdf[display_cols], use_container_width=True, hide_index=True)

    # ── TAB 3: OPEN POSITIONS ─────────────────────────────────────────────
    with tab_open:
        st.caption("Live view of all open positions + trades resolved in the last 24h  |  Read-only")

        # ── Check Positions button ────────────────────────────────────────
        st.markdown("#### Run check-positions")
        _run_all = st.button("🔄 Check All Positions", key="btn_check_all_positions")

        if _run_all:
            import subprocess, sys, os
            _env = {**os.environ}  # inherits all .env vars already loaded
            _jobs = [
                ("📈 Spot",    "paper_trade_executor.py"),
                ("⚡ Futures", "futures_trade_executor.py"),
            ]

            for _label, _script in _jobs:
                with st.spinner(f"Running {_label} check-positions…"):
                    try:
                        _proc = subprocess.run(
                            [sys.executable, _script, "--check-positions"],
                            capture_output=True, text=True, timeout=120,
                            cwd=str(Path(__file__).parent),
                            env=_env,
                        )
                        _stdout = _proc.stdout.strip()
                        _stderr = _proc.stderr.strip()
                        _rc     = _proc.returncode
                    except subprocess.TimeoutExpired:
                        _stdout, _stderr, _rc = "", "Timed out after 120 s", 1
                    except Exception as _exc:
                        _stdout, _stderr, _rc = "", str(_exc), 1

                if _rc == 0:
                    st.success(f"✅ {_label} check-positions completed")
                else:
                    st.error(f"❌ {_label} check-positions exited with code {_rc}")

                with st.expander(f"{_label} Output", expanded=(_rc != 0)):
                    if _stdout:
                        st.code(_stdout, language="text")
                    if _stderr:
                        st.warning("stderr:")
                        st.code(_stderr, language="text")
                    if not _stdout and not _stderr:
                        st.write("(no output)")

            # Reload data so cards reflect any newly resolved positions
            st.rerun()

        st.divider()

        # Load raw rows (not the processed DataFrames) so we have all original fields
        try:
            with global_state.lock:
                if global_state.spot_rows is None:
                    global_state.spot_rows = fetch_all_spot()
                _spot_rows = list(global_state.spot_rows)
        except Exception as exc:
            st.error("Failed to load spot trades for Open Positions:")
            st.exception(exc)
            _spot_rows = []

        try:
            with global_state.lock:
                if global_state.futures_rows is None:
                    global_state.futures_rows = fetch_all_futures()
                _futures_rows = list(global_state.futures_rows)
        except Exception as exc:
            st.error("Failed to load futures trades for Open Positions:")
            st.exception(exc)
            _futures_rows = []

        render_open_positions_tab(_spot_rows, _futures_rows)

    # ── TAB 4: ML SHADOW METRICS ─────────────────────────────────────────
    with tab_ml:
        st.caption("Model: `ml/models/v2.pkl` · Version: `v2.0.0` · Mode: **Observation Only** (no veto)")
        
        df_spot_ml = load_trade_data()
        
        if df_spot_ml.empty:
            st.info("No spot trades found.")
        else:
            has_ml = df_spot_ml["ml_score"].notna()
            scored = df_spot_ml[has_ml].copy()
            unscored = df_spot_ml[~has_ml].copy()

            st.divider()

            # ── KPIs ─────────────────────────────────────────────────────
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Scored Trades", len(scored))
            m2.metric("Unscored (Legacy)", len(unscored))

            scored_resolved = scored[scored["is_resolved"]].copy()
            if not scored_resolved.empty:
                scored_wr = scored_resolved["is_win"].mean() * 100
                avg_score = scored["ml_score"].mean()
                m3.metric("Scored Win Rate", f"{scored_wr:.1f}%")
                m4.metric("Avg ML Score", f"{avg_score:.3f}")
            else:
                m3.metric("Scored Win Rate", "—")
                m4.metric("Avg ML Score", f"{scored['ml_score'].mean():.3f}" if not scored.empty else "—")

            st.divider()

            if scored_resolved.empty:
                st.warning("No resolved trades with ML scores yet. Waiting for more data...")
            else:
                col_left, col_right = st.columns(2)

                # ── Score Distribution: Win vs Loss ──────────────────────
                with col_left:
                    st.markdown("#### Score Distribution by Outcome")
                    scored_resolved["Outcome"] = scored_resolved["is_win"].map(
                        {True: "✅ Win (TP Hit)", False: "🔴 Loss (SL Hit)"}
                    )
                    fig_dist = px.histogram(
                        scored_resolved, x="ml_score", color="Outcome",
                        nbins=20, barmode="overlay", opacity=0.7,
                        color_discrete_map={
                            "✅ Win (TP Hit)": "#2ca02c",
                            "🔴 Loss (SL Hit)": "#d62728"
                        },
                        labels={"ml_score": "ML Score (P(win))"},
                    )
                    fig_dist.update_layout(
                        template="plotly_white",
                        xaxis=dict(range=[0, 1]),
                        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
                    )
                    st.plotly_chart(fig_dist, use_container_width=True)

                # ── Box Plot ─────────────────────────────────────────────
                with col_right:
                    st.markdown("#### Score Box Plot by Outcome")
                    fig_box = px.box(
                        scored_resolved, x="Outcome", y="ml_score",
                        color="Outcome",
                        color_discrete_map={
                            "✅ Win (TP Hit)": "#2ca02c",
                            "🔴 Loss (SL Hit)": "#d62728"
                        },
                        labels={"ml_score": "ML Score"},
                    )
                    fig_box.update_layout(template="plotly_white", showlegend=False)
                    st.plotly_chart(fig_box, use_container_width=True)

                # ── Threshold Simulation ─────────────────────────────────
                st.markdown("#### 🎯 Threshold Simulation (What-If)")
                st.caption("If we had used the ML score to filter trades, what would be the impact?")

                thresh_data = []
                for thresh in np.arange(0.1, 0.95, 0.05):
                    approved = scored_resolved[scored_resolved["ml_score"] >= thresh]
                    n_app = len(approved)
                    if n_app == 0:
                        continue
                    wr = approved["is_win"].mean() * 100
                    pnl = approved["realized_pnl_usd"].sum()
                    filtered = len(scored_resolved) - n_app
                    thresh_data.append({
                        "Threshold": f"≥ {thresh:.2f}",
                        "Trades": n_app,
                        "Filtered": filtered,
                        "Win Rate (%)": round(wr, 1),
                        "Total PnL ($)": round(pnl, 2),
                    })

                if thresh_data:
                    st.dataframe(pd.DataFrame(thresh_data), use_container_width=True, hide_index=True)

                # ── Score vs PnL scatter ─────────────────────────────────
                st.markdown("#### ML Score vs Realized PnL")
                fig_scatter = px.scatter(
                    scored_resolved, x="ml_score", y="realized_pnl_pct",
                    color="Outcome",
                    color_discrete_map={
                        "✅ Win (TP Hit)": "#2ca02c",
                        "🔴 Loss (SL Hit)": "#d62728"
                    },
                    hover_name="symbol",
                    labels={"ml_score": "ML Score", "realized_pnl_pct": "Realized PnL (%)"},
                )
                fig_scatter.add_vline(x=0.5, line_dash="dash", line_color="gray",
                                      annotation_text="Default threshold")
                fig_scatter.update_layout(template="plotly_white")
                st.plotly_chart(fig_scatter, use_container_width=True)

                # ── Diagnostic Summary ───────────────────────────────────
                st.divider()
                st.markdown("#### 📊 Diagnostic Summary")

                wins_scored = scored_resolved[scored_resolved["is_win"]]
                losses_scored = scored_resolved[~scored_resolved["is_win"]]
                avg_win_score = wins_scored["ml_score"].mean() if not wins_scored.empty else float("nan")
                avg_loss_score = losses_scored["ml_score"].mean() if not losses_scored.empty else float("nan")

                d1, d2, d3 = st.columns(3)
                d1.metric("Avg Score (Wins)", f"{avg_win_score:.3f}" if not pd.isna(avg_win_score) else "—")
                d2.metric("Avg Score (Losses)", f"{avg_loss_score:.3f}" if not pd.isna(avg_loss_score) else "—")
                score_gap = avg_win_score - avg_loss_score if not (pd.isna(avg_win_score) or pd.isna(avg_loss_score)) else 0
                d3.metric("Score Gap (Win - Loss)", f"{score_gap:+.3f}")

                if abs(score_gap) < 0.05:
                    st.warning("⚠️ **Score gap < 0.05** — Model cannot distinguish wins from losses. "
                               "ROC-AUC ≈ 0.50 (random). Shadow scoring is collecting data only.")
                elif score_gap > 0.05:
                    st.success("✅ **Positive score gap detected.** Monitor ROC-AUC trend as N grows.")
                else:
                    st.error("🔴 **Negative score gap** — Model is inversely correlated. Needs retraining.")

                st.caption(
                    "ℹ️ ML v2 Shadow Scoring is **Spot-only**. "
                    "Futures pipeline runs independently without ML scoring. "
                    "A dedicated Futures ML model will be trained when Effective N > 100."
                )

    # Independent timer-based sync is handled by st_autorefresh above.
    # Keep a lightweight fallback here to invalidate cached rows after the
    # expected sync point, even if websocket events were missed.
    @st.fragment(run_every=60)
    def _scheduled_refresh_watcher():
        try:
            hb = _heartbeat_for_timer
            if not hb:
                return

            next_expected_wib = _parse_iso_to_wib(hb.get("next_expected_at"))
            if next_expected_wib is None:
                return

            refresh_target_wib = next_expected_wib + timedelta(seconds=NORMAL_REFRESH_BUFFER_SECONDS)
            if now_wib >= refresh_target_wib:
                with global_state.lock:
                    global_state.spot_rows = None
                    global_state.futures_rows = None
                global_state.pending_rerun = False
                st.rerun()
        except Exception:
            pass

    _scheduled_refresh_watcher()

    # ── WebSocket fallback: immediate rerun if WS event arrived ──────
    @st.fragment(run_every=3)
    def _realtime_watcher():
        if global_state.pending_rerun:
            global_state.pending_rerun = False
            st.session_state["_last_seen_update"] = global_state.last_updated
            st.rerun()

    _realtime_watcher()


if __name__ == "__main__":
    main()
