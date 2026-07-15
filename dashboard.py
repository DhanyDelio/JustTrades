import math
import os

import pandas as pd
import plotly.express as px
import streamlit as st
from pathlib import Path

from supabase_client import fetch_all_spot, fetch_all_futures


st.set_page_config(page_title="Swing Trade Dashboard", layout="wide")


STARTING_LAB_CAPITAL = 240.0


# ---------------------------------------------------------------------------
# SPOT — data loading + helpers  (unchanged from original)
# ---------------------------------------------------------------------------

def load_trade_data() -> pd.DataFrame:
    try:
        rows = fetch_all_spot()
    except Exception as exc:
        st.error(f"Failed to load spot trades from Supabase: {exc}")
        st.stop()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["realized_pnl_usd", "realized_pnl_pct", "planned_rr",
                "entry_price", "entry_fill_price", "sl", "tp1",
                "entry_qty", "entry_notional", "budget_usd"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["is_resolved"] = df["exit_status"].fillna("").astype(str).str.upper().isin(["TP_HIT", "SL_HIT"])
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
    try:
        rows = fetch_all_futures()
    except Exception as exc:
        st.error(f"Failed to load futures trades from Supabase: {exc}")
        return pd.DataFrame()

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

    df["is_resolved"] = df["exit_status"].fillna("").astype(str).str.upper().isin(["TP_HIT", "SL_HIT"])
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
    Fetch current prices using Binance Spot Testnet (same client as
    paper_trade_executor.py). Falls back to public mainnet if testnet
    keys are not configured.
    """
    prices: dict[str, float] = {}
    if not symbols:
        return prices

    sym_set = set(symbols)

    # ── Primary: Binance Spot Testnet (no SSL/Cloudflare issues) ───────
    try:
        api_key    = os.getenv("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
        if api_key and api_secret:
            from binance.client import Client as _Client
            _c = _Client(api_key, api_secret, testnet=True)
            tickers = _c.get_all_tickers()
            for item in tickers:
                if item.get("symbol") in sym_set:
                    prices[item["symbol"]] = float(item["price"])
    except Exception:
        pass

    # ── Fallback: public mainnet (may fail on some networks) ───────────
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
        "CANCELED":         "❌ CANCELED",
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
    status_color = {"FILLED": "#2ca02c", "NEW": "#ff7f0e", "PARTIALLY_FILLED": "#1f77b4"}.get(entry_status, "#888")
    status_icon  = {"FILLED": "✅", "NEW": "🕐", "PARTIALLY_FILLED": "🔄"}.get(entry_status, "❓")
    oco_ok       = oco_placed and oco_list_id
    oco_badge    = (f"<span style='background:#1a7a1a;color:#fff;border-radius:4px;"
                    f"padding:1px 7px;font-size:0.78em'>OCO ✓</span>"
                    if oco_ok else
                    (f"<span style='background:#7a1a1a;color:#fff;border-radius:4px;"
                     f"padding:1px 7px;font-size:0.78em'>⚠ NO OCO</span>"
                     if is_filled else ""))

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
        else:
            st.markdown(
                f"<div style='background:#f5f5f5;border-radius:6px;padding:6px 12px;margin-bottom:8px'>"
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
        else:
            st.markdown(
                f"<div style='background:#f5f5f5;border-radius:6px;padding:6px 12px;margin-bottom:8px'>"
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
    pnl_color   = "#2ca02c" if is_win else "#d62728"
    border_css  = f"border-left: 4px solid {pnl_color}; padding-left: 10px;"
    icon        = "🟢" if is_win else "🔴"
    label       = "TP HIT" if is_win else "SL HIT"

    # Exit time
    exit_time_raw = trade.get("exit_time")
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
            return False
        try:
            return float(et) >= cutoff_ms
        except (TypeError, ValueError):
            return False

    spot_resolved    = [t for t in spot_rows
                        if t.get("exit_status") in ("TP_HIT", "SL_HIT") and _is_recent(t)]
    futures_resolved = [t for t in futures_rows
                        if t.get("exit_status") in ("TP_HIT", "SL_HIT") and _is_recent(t)]

    # Sort resolved by exit_time desc (most recent first)
    spot_resolved.sort(   key=lambda t: float(t.get("exit_time") or 0), reverse=True)
    futures_resolved.sort(key=lambda t: float(t.get("exit_time") or 0), reverse=True)

    # ── Fetch live prices for open positions ──────────────────────────
    spot_syms    = list({t["symbol"] for t in spot_open    if t.get("symbol")})
    futures_syms = list({t["symbol"] for t in futures_open if t.get("symbol")})

    with st.spinner("Fetching live prices..."):
        spot_prices    = _fetch_spot_prices(spot_syms)
        futures_prices = _fetch_futures_prices(futures_syms)

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

    if st.button("🔄 Refresh data"):
        st.rerun()

    tab_spot, tab_futures, tab_open = st.tabs(["📈 Spot", "⚡ Futures", "📋 Open Positions"])

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

        # ── Check Positions buttons ───────────────────────────────────────
        st.markdown("#### Run check-positions")
        _cp_col1, _cp_col2, _cp_col3 = st.columns([1, 1, 3])
        _run_spot    = _cp_col1.button("📈 Check Spot",    key="btn_check_spot")
        _run_futures = _cp_col2.button("⚡ Check Futures", key="btn_check_futures")

        if _run_spot or _run_futures:
            import subprocess, sys, os
            _script = (
                "paper_trade_executor.py"   if _run_spot
                else "futures_trade_executor.py"
            )
            _label  = "Spot" if _run_spot else "Futures"
            _env    = {**os.environ}  # inherits all .env vars already loaded

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

            with st.expander("Output", expanded=True):
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
            _spot_rows = fetch_all_spot()
        except Exception as exc:
            st.error(f"Failed to load spot trades: {exc}")
            _spot_rows = []

        try:
            _futures_rows = fetch_all_futures()
        except Exception as exc:
            st.error(f"Failed to load futures trades: {exc}")
            _futures_rows = []

        render_open_positions_tab(_spot_rows, _futures_rows)


if __name__ == "__main__":
    main()
