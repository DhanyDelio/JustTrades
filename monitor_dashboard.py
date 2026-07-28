"""
monitor_dashboard.py — Local Monitoring Dashboard (MacBook)
=============================================================
Lightweight Streamlit dashboard for monitoring live bot activity.
Reads data from Supabase (same .env as the trading bot).

Usage:
    streamlit run monitor_dashboard.py

Features:
    - Spot Overview: 20 most recent positions with status, PnL, OCO status
    - Futures Overview: 20 most recent positions with side, leverage, PnL
    - ML Shadow Metrics: v2 score distribution vs actual trade outcomes
    - Auto-refresh every 30 seconds
"""

import math
import os
import pytz
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from services.supabase_client import fetch_all_spot, fetch_all_futures

# ── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Swing Trade Monitor",
    page_icon="📡",
    layout="wide",
)

# ── Auto-refresh (30 seconds) ────────────────────────────────────────────
refresh_count = st_autorefresh(interval=30_000, limit=None, key="monitor_refresh")

# ── Styling ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Dark theme overrides */
    .stMetric > div { padding: 8px 12px; border-radius: 8px; }
    .status-open { color: #f0ad4e; font-weight: bold; }
    .status-filled { color: #5bc0de; font-weight: bold; }
    .status-tp { color: #5cb85c; font-weight: bold; }
    .status-sl { color: #d9534f; font-weight: bold; }
    .pnl-positive { color: #5cb85c; }
    .pnl-negative { color: #d9534f; }
    div[data-testid="stMetricValue"] { font-size: 1.3rem; }
</style>
""", unsafe_allow_html=True)

# ── Helpers ──────────────────────────────────────────────────────────────

def parse_epoch_ms(series):
    values = pd.to_numeric(series, errors="coerce")
    return pd.to_datetime(values, unit="ms", utc=True, errors="coerce")


def format_pnl(val):
    if pd.isna(val):
        return "—"
    sign = "+" if val >= 0 else ""
    color = "green" if val >= 0 else "red"
    return f":{color}[{sign}{val:.2f}]"


def format_status(status):
    if not status or pd.isna(status):
        return "⏳ PENDING"
    s = str(status).upper()
    if s == "TP_HIT":
        return "✅ TP HIT"
    elif s == "SL_HIT":
        return "🔴 SL HIT"
    elif s == "CANCELED":
        return "⚪ CANCELED"
    elif s == "FILLED":
        return "🔵 FILLED"
    return f"⏳ {s}"


def time_ago(dt):
    """Human-readable time-ago string from a datetime."""
    if pd.isna(dt):
        return "—"
    now = datetime.now(pytz.UTC)
    try:
        diff = now - dt
    except TypeError:
        return "—"
    hours = diff.total_seconds() / 3600
    if hours < 1:
        return f"{int(diff.total_seconds() / 60)}m ago"
    elif hours < 24:
        return f"{int(hours)}h ago"
    else:
        return f"{int(hours / 24)}d ago"


# ── Data loading ─────────────────────────────────────────────────────────

@st.cache_data(ttl=25)
def load_spot() -> pd.DataFrame:
    rows = fetch_all_spot()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in ["realized_pnl_usd", "realized_pnl_pct", "planned_rr",
                "entry_price", "entry_fill_price", "sl", "tp1",
                "entry_qty", "entry_notional", "budget_usd", "ml_score"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["exit_status_clean"] = df["exit_status"].fillna("").astype(str).str.upper()
    df["is_resolved"] = df["exit_status_clean"].isin(["TP_HIT", "SL_HIT", "CANCELED"])
    df["is_win"] = df["realized_pnl_usd"].gt(0)
    df["entry_fill_dt"] = parse_epoch_ms(df.get("entry_fill_time"))
    df["exit_dt"] = parse_epoch_ms(df.get("exit_time"))
    return df


@st.cache_data(ttl=25)
def load_futures() -> pd.DataFrame:
    rows = fetch_all_futures()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in ["realized_pnl_usd", "realized_pnl_pct", "planned_rr",
                "entry_price", "entry_fill_price", "sl", "tp1",
                "entry_qty", "entry_notional", "margin_used",
                "leverage", "liquidation_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["exit_status_clean"] = df["exit_status"].fillna("").astype(str).str.upper()
    df["is_resolved"] = df["exit_status_clean"].isin(["TP_HIT", "SL_HIT", "CANCELED"])
    df["is_win"] = df["realized_pnl_usd"].gt(0)
    df["exit_dt"] = parse_epoch_ms(df.get("exit_time"))
    if "position_side" not in df.columns:
        df["position_side"] = "UNKNOWN"
    return df


# ══════════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════════

now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%d %b %Y — %H:%M:%S WIB")
st.markdown(f"## 📡 Swing Trade Monitor")
st.caption(f"Last refresh: **{now_wib}** · Auto-refresh every 30s")

# ══════════════════════════════════════════════════════════════════════════
# TAB LAYOUT
# ══════════════════════════════════════════════════════════════════════════

tab_spot, tab_futures, tab_ml = st.tabs([
    "🟢 Spot Overview",
    "🟠 Futures Overview",
    "🧪 ML Shadow Metrics",
])

# ══════════════════════════════════════════════════════════════════════════
# TAB 1: SPOT OVERVIEW
# ══════════════════════════════════════════════════════════════════════════

with tab_spot:
    df_spot = load_spot()

    if df_spot.empty:
        st.info("No spot trades found in Supabase.")
    else:
        # ── KPI row ──────────────────────────────────────────────────
        resolved_spot = df_spot[df_spot["is_resolved"]]
        total_pnl = resolved_spot["realized_pnl_usd"].sum() if not resolved_spot.empty else 0
        win_rate = resolved_spot["is_win"].mean() * 100 if not resolved_spot.empty else 0
        open_count = int((~df_spot["is_resolved"]).sum())

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Trades", len(df_spot))
        k2.metric("Open Positions", open_count)
        k3.metric("Win Rate", f"{win_rate:.1f}%")
        k4.metric("Total PnL", f"${total_pnl:+.2f}")

        st.divider()

        # ── Recent 20 positions table ────────────────────────────────
        st.subheader("📋 20 Most Recent Spot Positions")

        display_cols = []
        recent = df_spot.sort_values("id", ascending=False).head(20).copy()

        recent["Status"] = recent["exit_status_clean"].apply(format_status)
        recent["PnL ($)"] = recent["realized_pnl_usd"].apply(
            lambda v: f"+{v:.2f}" if pd.notna(v) and v >= 0 else (f"{v:.2f}" if pd.notna(v) else "—")
        )
        recent["PnL (%)"] = recent["realized_pnl_pct"].apply(
            lambda v: f"+{v:.2f}%" if pd.notna(v) and v >= 0 else (f"{v:.2f}%" if pd.notna(v) else "—")
        )
        recent["Cluster"] = recent.get("correlation_cluster_id", pd.Series(dtype=str)).fillna("—").apply(
            lambda x: x[:8] + "…" if isinstance(x, str) and len(x) > 8 and x != "—" else x
        )
        recent["Entry"] = recent["entry_fill_price"].apply(
            lambda v: f"${v:.4f}" if pd.notna(v) else "⏳"
        )
        recent["SL"] = recent["sl"].apply(lambda v: f"${v:.4f}" if pd.notna(v) else "—")
        recent["TP"] = recent["tp1"].apply(lambda v: f"${v:.4f}" if pd.notna(v) else "—")
        recent["R:R"] = recent["planned_rr"].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "—")
        recent["ML Score"] = recent.get("ml_score", pd.Series(dtype=float)).apply(
            lambda v: f"{v:.3f}" if pd.notna(v) else "—"
        )
        recent["Age"] = recent["entry_fill_dt"].apply(time_ago)

        show_cols = ["symbol", "Status", "Cluster", "Entry", "SL", "TP", "R:R",
                     "PnL ($)", "PnL (%)", "ML Score", "Age"]
        st.dataframe(
            recent[show_cols].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
        )

        # ── Cluster breakdown ────────────────────────────────────────
        with st.expander("📦 Cluster Breakdown"):
            clusters = df_spot[df_spot["correlation_cluster_id"].notna()]
            if clusters.empty:
                st.caption("No cluster trades yet.")
            else:
                cluster_summary = []
                for cid, grp in clusters.groupby("correlation_cluster_id"):
                    res = grp[grp["is_resolved"]]
                    cluster_summary.append({
                        "Cluster ID": cid[:12] + "…" if len(str(cid)) > 12 else cid,
                        "Trades": len(grp),
                        "Resolved": len(res),
                        "PnL ($)": round(res["realized_pnl_usd"].sum(), 2) if not res.empty else 0,
                        "Win Rate": f"{res['is_win'].mean()*100:.0f}%" if not res.empty else "—",
                    })
                st.dataframe(pd.DataFrame(cluster_summary), use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════
# TAB 2: FUTURES OVERVIEW
# ══════════════════════════════════════════════════════════════════════════

with tab_futures:
    df_fut = load_futures()

    if df_fut.empty:
        st.info("No futures trades found in Supabase.")
    else:
        # ── KPI row ──────────────────────────────────────────────────
        resolved_fut = df_fut[df_fut["is_resolved"]]
        total_pnl_f = resolved_fut["realized_pnl_usd"].sum() if not resolved_fut.empty else 0
        win_rate_f = resolved_fut["is_win"].mean() * 100 if not resolved_fut.empty else 0
        open_count_f = int((~df_fut["is_resolved"]).sum())

        # Side breakdown
        long_trades = resolved_fut[resolved_fut["position_side"] == "LONG"]
        short_trades = resolved_fut[resolved_fut["position_side"] == "SHORT"]

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Trades", len(df_fut))
        k2.metric("Open Positions", open_count_f)
        k3.metric("Win Rate", f"{win_rate_f:.1f}%")
        k4.metric("Total PnL", f"${total_pnl_f:+.2f}")

        # Side KPIs
        s1, s2 = st.columns(2)
        with s1:
            long_wr = long_trades["is_win"].mean() * 100 if not long_trades.empty else 0
            long_pnl = long_trades["realized_pnl_usd"].sum() if not long_trades.empty else 0
            st.metric("🟢 LONG", f"{len(long_trades)} trades · {long_wr:.0f}% WR · ${long_pnl:+.2f}")
        with s2:
            short_wr = short_trades["is_win"].mean() * 100 if not short_trades.empty else 0
            short_pnl = short_trades["realized_pnl_usd"].sum() if not short_trades.empty else 0
            st.metric("🔴 SHORT", f"{len(short_trades)} trades · {short_wr:.0f}% WR · ${short_pnl:+.2f}")

        st.divider()

        # ── Recent 20 positions table ────────────────────────────────
        st.subheader("📋 20 Most Recent Futures Positions")

        recent_f = df_fut.sort_values("id", ascending=False).head(20).copy()

        recent_f["Status"] = recent_f["exit_status_clean"].apply(format_status)
        recent_f["Side"] = recent_f["position_side"].fillna("—")
        recent_f["Lev"] = recent_f["leverage"].apply(
            lambda v: f"{int(v)}x" if pd.notna(v) else "3x"
        )
        recent_f["PnL ($)"] = recent_f["realized_pnl_usd"].apply(
            lambda v: f"+{v:.2f}" if pd.notna(v) and v >= 0 else (f"{v:.2f}" if pd.notna(v) else "—")
        )
        recent_f["PnL (%)"] = recent_f["realized_pnl_pct"].apply(
            lambda v: f"+{v:.2f}%" if pd.notna(v) and v >= 0 else (f"{v:.2f}%" if pd.notna(v) else "—")
        )
        recent_f["Entry"] = recent_f["entry_fill_price"].apply(
            lambda v: f"${v:.4f}" if pd.notna(v) else "⏳"
        )
        recent_f["SL"] = recent_f["sl"].apply(lambda v: f"${v:.4f}" if pd.notna(v) else "—")
        recent_f["TP"] = recent_f["tp1"].apply(lambda v: f"${v:.4f}" if pd.notna(v) else "—")
        recent_f["R:R"] = recent_f["planned_rr"].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "—")

        show_cols_f = ["symbol", "Side", "Lev", "Status", "Entry", "SL", "TP",
                       "R:R", "PnL ($)", "PnL (%)"]
        st.dataframe(
            recent_f[show_cols_f].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
        )

        # ── Equity curve ─────────────────────────────────────────────
        with st.expander("📈 Equity Curve (by Side)"):
            if not resolved_fut.empty:
                eq_data = []
                for side in resolved_fut["position_side"].dropna().unique():
                    sub = resolved_fut[resolved_fut["position_side"] == side].sort_values("exit_dt").copy()
                    sub["cum_pnl"] = sub["realized_pnl_usd"].cumsum()
                    sub["Side"] = side
                    eq_data.append(sub[["exit_dt", "cum_pnl", "Side"]])
                if eq_data:
                    eq_df = pd.concat(eq_data, ignore_index=True)
                    fig = px.line(eq_df, x="exit_dt", y="cum_pnl", color="Side", markers=True,
                                 labels={"exit_dt": "Exit Time", "cum_pnl": "Cumulative PnL ($)"})
                    fig.update_layout(template="plotly_white")
                    st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("No resolved futures trades yet.")

        st.caption("⚠️ Futures pipeline is **rule-based only** — no ML model applied. "
                   "ML scoring will be added once Effective N > 100.")


# ══════════════════════════════════════════════════════════════════════════
# TAB 3: ML SHADOW METRICS
# ══════════════════════════════════════════════════════════════════════════

with tab_ml:
    df_spot_ml = load_spot()

    if df_spot_ml.empty:
        st.info("No spot trades found.")
    else:
        has_ml = df_spot_ml["ml_score"].notna()
        scored = df_spot_ml[has_ml].copy()
        unscored = df_spot_ml[~has_ml].copy()

        st.subheader("🧪 ML v2 Shadow Scoring Analysis (Spot Only)")
        st.caption("Model: `ml/models/v2.pkl` · Version: `v2.0.0` · Mode: **Observation Only** (no veto)")

        st.divider()

        # ── KPIs ─────────────────────────────────────────────────────
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Scored Trades", len(scored))
        m2.metric("Unscored (Legacy)", len(unscored))

        scored_resolved = scored[scored["is_resolved"]]
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
