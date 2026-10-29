"""Wise Stocks Optionaire — trading agent console (Streamlit).

Paper-trading implementation of the WSO rule set: regime → screen →
specialists → critic → deterministic risk engine → (human approval) → paper
execution → monitoring → email alerts and daily/weekly reports.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from wso import policy as P
from wso.data import is_market_open, now_ny
from wso.notify import send_email, transport_status, valid_email, wrap
from wso.paper_broker import PaperBroker
from wso.pipeline import approve_proposal, log, monitor, reject_proposal, run_cycle
from wso.reports import build_report, performance, send_report
from wso.risk_engine import account_view
from wso.scoring import WEIGHTS
from wso.store import get_store

st.set_page_config(page_title="WSO Trading Agent", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

ACCENT = "#0f766e"
st.markdown(f"""
<style>
.block-container {{padding-top: 1.4rem;}}
.wso-card {{border:1px solid #e5e7eb;border-radius:12px;padding:14px 16px;background:#fff;}}
.wso-muted {{color:#6b7280;font-size:0.85rem;}}
.wso-pill {{display:inline-block;padding:2px 10px;border-radius:999px;font-size:0.78rem;font-weight:600;}}
.pill-on {{background:#dcfce7;color:#166534;}} .pill-off {{background:#fee2e2;color:#991b1b;}} .pill-info {{background:#e0f2fe;color:#075985;}}
div[data-testid="stMetricValue"] {{font-size:1.4rem;}}
</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------------ state
@st.cache_resource
def _store():
    return get_store()


def load_state() -> dict:
    if "state" not in st.session_state:
        st.session_state.state = _store().load()
    return st.session_state.state


def save_state():
    _store().save(st.session_state.state)


def refresh_state():
    st.session_state.state = _store().load()


S = load_state()
POL = P.merged(S["settings"].get("policy_overrides"))


def pill(text, kind):
    return f"<span class='wso-pill pill-{kind}'>{text}</span>"


def fmt_money(x):
    return f"${x:,.2f}"


def md(text: str) -> str:
    """Escape dollar signs so Streamlit's markdown does not treat them as LaTeX."""
    return text.replace("$", "\\$")


# --------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### Wise Stocks Optionaire")
    st.caption("Rule-bound swing-trading agent · paper mode")
    acct = account_view(S)
    st.metric("Paper equity", fmt_money(acct["equity"]), f"{acct['equity']-S['account']['starting_capital']:+,.2f}")
    st.markdown(
        pill("Human approval ON" if S["settings"]["human_approval"] else "Auto mode", "on" if S["settings"]["human_approval"] else "off") + " " +
        pill("Alerts ON" if S["settings"]["notifications_enabled"] else "Alerts OFF", "on" if S["settings"]["notifications_enabled"] else "off") + " " +
        (pill("KILL SWITCH", "off") if S["settings"]["kill_switch"] else pill("Market open" if is_market_open() else "Market closed", "info")),
        unsafe_allow_html=True)
    reg = S.get("regime") or {}
    st.caption(f"Regime: **{reg.get('regime', 'not computed')}** ({reg.get('score', '—')}) · as of close {reg.get('as_of_close', '—')}")
    st.caption(f"New York time {now_ny().strftime('%a %H:%M')} · store: {_store().name}")
    if st.button("Reload state", use_container_width=True):
        refresh_state()
        st.rerun()


# ------------------------------------------------------------------ pages
def page_dashboard():
    st.title("Dashboard")
    acct = account_view(S)
    perf = performance(S, dt.date.today())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity", fmt_money(acct["equity"]), f"{perf['total_return_pct']:+.2f}% since start")
    c2.metric("Cash", fmt_money(acct["cash"]))
    c3.metric("Open positions", f"{len(S['positions'])} / {POL['max_positions']}")
    c4.metric("Portfolio heat", f"{perf['heat_pct']:.2f}%", f"limit {POL['max_portfolio_heat_pct']:.0f}%", delta_color="off")
    c5.metric("Win rate", f"{perf['win_rate']:.0f}%", f"{perf['total_trades']} closed", delta_color="off")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Equity curve")
        curve = S["account"]["equity_curve"]
        if curve:
            df = pd.DataFrame(curve)
            fig = go.Figure(go.Scatter(x=df["date"], y=df["equity"], mode="lines+markers", line=dict(color=ACCENT, width=2)))
            fig.add_hline(y=S["account"]["starting_capital"], line_dash="dot", line_color="#9ca3af")
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="USD", xaxis_title=None)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No equity history yet. Run the pipeline or the monitor once to start the curve.")
    with right:
        st.subheader("Market regime")
        if reg:
            st.markdown(f"**{reg['regime'].replace('_', ' ').title()}** · score {reg['score']} · size tier "
                        f"{P.size_pct_for_regime(POL, reg['regime']):.0f}% of equity per trade")
            st.write("Leaders: " + ", ".join(reg["leaders"]))
            st.write("Laggards: " + ", ".join(reg["laggards"]))
            for n in reg.get("notes", []):
                st.caption("• " + n)
            if reg.get("facts", {}).get("VIX"):
                st.caption(f"VIX {reg['facts']['VIX']} · breadth {reg['facts'].get('breadth_pct_above_48ema', '—')}% above 48 EMA")
        else:
            st.info("Run the pipeline to compute the regime.")

    lc = S.get("last_cycle") or {}
    if lc.get("data_quality") == "degraded":
        st.warning("Last cycle: the data vendor rate-limited fundamentals for most names, so scores were computed "
                   "with neutral fundamental factors and all decisions were capped at WATCHLIST. Re-run the pipeline later.")

    st.subheader("Pending proposals")
    pend = [p for p in S["pending_proposals"] if p["status"] == "pending_approval"]
    if pend:
        _proposal_cards(pend)
    else:
        st.caption("None. Proposals appear here when a candidate clears score ≥ 75 and reward:risk ≥ 2.0.")

    st.subheader("Open positions")
    _positions_table()

    st.subheader("Latest decisions")
    _log_table(S["decision_log"][-12:][::-1])


def _proposal_cards(pend):
    for p in pend:
        with st.container(border=True):
            a, b, c = st.columns([3, 3, 2])
            with a:
                st.markdown(f"### {p['symbol']} · {p['strategy'].replace('_', ' ')}")
                st.markdown(md(f"Buy **{p['quantity']} sh** limit **≤ ${p['limit_price']:.2f}** (ref ${p['reference_price']:.2f}) · "
                               f"position ${p['position_value']:,.2f} ({p['size_pct']:.1f}% of equity)"))
                st.markdown(md(f"Stop **${p['stop_price']:.2f}** ({100*(1-p['stop_price']/p['limit_price']):.1f}% of position, max loss ${p['max_loss']:,.2f}) · "
                               f"T1 ${p['target_1']:.2f} · T2 ${p['target_2']:.2f} · R:R {p['expected_rr']}"))
                st.caption(f"WSO score {p['score']} · decision {p['decision']} · regime {p['market_regime']} · created {p['created_at_utc'][:16]} UTC")
            with b:
                st.markdown("**Critic bear case**")
                for x in p.get("bear_case", [])[:4]:
                    st.caption("• " + x)
                with st.expander("Risk-engine checks"):
                    for s_, m in p.get("risk_checks", []):
                        st.write(("✅ " if s_ == "PASS" else "ℹ️ " if s_ == "INFO" else "❌ ") + m)
            with c:
                if not is_market_open():
                    st.warning("Market closed — orders submit 9:30–16:00 ET.")
                if st.button(f"Approve {p['symbol']}", key=f"ap_{p['id']}", type="primary", use_container_width=True):
                    res = approve_proposal(S, p["id"], approver="site")
                    save_state()
                    (st.success if res["ok"] else st.error)(f"{p['symbol']}: " + ("order " + res["order"]["status"] if res["ok"] else res["error"]))
                    st.rerun()
                if st.button(f"Decline {p['symbol']}", key=f"rj_{p['id']}", use_container_width=True):
                    reject_proposal(S, p["id"])
                    save_state()
                    st.rerun()


def _positions_table():
    if not S["positions"]:
        st.caption("No open positions.")
        return
    rows = []
    for p in S["positions"]:
        last = p.get("last_price", p["entry_price"])
        rows.append({"Ticker": p["symbol"], "Qty": p["quantity"], "Entry": p["entry_price"], "Last": last,
                     "Unrealized $": round((last - p["entry_price"]) * p["quantity"], 2), "Unrealized %": round(100 * (last / p["entry_price"] - 1), 2),
                     "Stop": p["stop"], "T1": p["target_1"], "T2": p["target_2"], "T1 hit": p["t1_hit"], "Setup": p["strategy"],
                     "Entered": p["entry_date"], "Score": p["score"]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _log_table(entries):
    if not entries:
        st.caption("Nothing logged yet.")
        return
    df = pd.DataFrame([{"Time (UTC)": e["at"][5:16].replace("T", " "), "Ticker": e.get("symbol") or "—", "Decision": e["decision"],
                        "Score": e.get("score", ""), "Reason": e["reason"], "Rule": e["rule"]} for e in entries])
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_pipeline():
    st.title("Run pipeline")
    st.caption("Gate 1 account → Gate 2 regime → Gate 3 screen → Gate 4 specialists → Gate 5 critic → Gate 6 risk engine → "
               "Gate 7 freshness → Gate 8 broker eligibility → Gate 9 approval → Gate 10 duplicate/kill switch")
    c1, c2, c3 = st.columns([1, 1, 3])
    run = c1.button("Run full cycle now", type="primary", use_container_width=True)
    mon = c2.button("Monitor positions", use_container_width=True)
    if S["settings"]["kill_switch"]:
        st.error("Kill switch is active — no new positions will be proposed. Turn it off in Settings.")
    if run:
        box = st.status("Running the WSO pipeline…", expanded=True)
        summ = run_cycle(S, progress=lambda m: box.write(m))
        save_state()
        box.update(label="Cycle complete", state="complete", expanded=False)
        if summ.get("halted"):
            st.warning(f"Halted: {summ['halted']}")
        st.success(f"Evaluated {summ.get('evaluated', 0)} names · {len(summ['proposals'])} proposal(s) · {len(summ['watchlist'])} on watchlist · "
                   f"{len(summ['rejected'])} rejected")
    if mon:
        with st.spinner("Checking fills, stops and targets…"):
            out = monitor(S)
            save_state()
        st.json({k: v for k, v in out.items() if k != "mark"})

    cands = S.get("last_candidates") or []
    if cands:
        st.subheader(f"Last evaluation — {len(cands)} candidates ({cands[0]['evaluated_at_utc'][:16]} UTC)")
        df = pd.DataFrame([{"Ticker": c["symbol"], "Sector": c["sector"], "Setup": c["setup"], "Price": c["price"], "Score": c["score"],
                            "Decision": c["decision"], "R:R": c["expected_rr"], "Stop": c["stop"], "Stop %": c["stop_pct"], "T1": c["t1"], "T2": c["t2"],
                            "Next earnings": c["earnings"]["next"], "Inside window": c["earnings"]["inside_window"],
                            "Reasons": "; ".join(c["reasons"])} for c in cands])
        st.dataframe(df, use_container_width=True, hide_index=True, height=min(600, 40 + 35 * len(df)))
        pick = st.selectbox("Inspect a candidate", [c["symbol"] for c in cands])
        c = next(x for x in cands if x["symbol"] == pick)
        a, b = st.columns(2)
        with a:
            st.markdown(f"#### {c['symbol']} — {c.get('name') or ''}")
            st.markdown(f"Decision **{c['decision']}** · score **{c['score']}** · setup {c['setup']}")
            st.markdown("**Factor scores**")
            st.dataframe(pd.DataFrame({"factor": list(c["factors"].keys()), "score": list(c["factors"].values()),
                                       "weight": [WEIGHTS[k] for k in c["factors"]]}),
                         hide_index=True, use_container_width=True)
            if c["penalties"]:
                st.markdown("**Penalties**: " + "; ".join(f"{p} (−{v})" for p, v in c["penalties"]))
        with b:
            st.markdown("**Technical (fact, computed from daily bars)**")
            t = c["tech"]
            st.json({k: t[k] for k in ["as_of", "price", "ema21", "ema48", "ema200", "atr14", "atr_pct", "ext_vs_21ema_pct", "pct_from_52w_high",
                                       "rs_1m_pct", "rs_3m_pct", "vol_ratio_last", "up_days_high_vol_20", "down_days_high_vol_20", "stop", "t1", "t2", "rr_t1", "rr_t2", "expected_rr", "t2_source"]})
            st.markdown("**Critic bear case**")
            for x in c["bear_case"]:
                st.caption("• " + x)
            st.markdown("**Earnings / fundamentals (vendor facts; None = unavailable, never invented)**")
            st.json({"earnings": c["earnings"], **c["fund"]})


def page_positions():
    st.title("Positions & orders")
    _positions_table()
    if S["positions"]:
        st.markdown("Manual exits (logged as user decisions):")
        for p in S["positions"]:
            a, b, c = st.columns([2, 1, 1])
            a.write(f"{p['symbol']} · {p['quantity']} sh · stop {p['stop']}")
            if b.button(f"Close {p['symbol']}", key=f"cl_{p['id']}"):
                broker = PaperBroker(S, POL)
                tr = broker.close(p, p.get("last_price", p["entry_price"]), "user_manual_exit")
                log(S, "EXIT", f"user manual exit P&L ${tr['pnl']}", "User override (logged)", symbol=p["symbol"])
                save_state()
                st.rerun()
            if c.button(f"Sell half {p['symbol']}", key=f"hf_{p['id']}"):
                broker = PaperBroker(S, POL)
                tr = broker.close(p, p.get("last_price", p["entry_price"]), "user_partial_exit", qty=round(p["quantity"] / 2, 4))
                log(S, "TAKE_PARTIAL_PROFIT", f"user partial exit P&L ${tr['pnl']}", "User override (logged)", symbol=p["symbol"])
                save_state()
                st.rerun()
    st.subheader("Closed trades")
    if S["trades"]:
        df = pd.DataFrame(S["trades"])[["symbol", "entry_date", "exit_date", "quantity", "entry_price", "exit_price", "pnl", "pnl_pct", "r_multiple", "reason", "strategy", "score"]]
        st.dataframe(df.iloc[::-1], use_container_width=True, hide_index=True)
    else:
        st.caption("No closed trades yet.")
    st.subheader("Orders")
    if S["orders"]:
        df = pd.DataFrame([{"Time": o["created_at_utc"][:16], "Ticker": o["symbol"], "Side": o["side"], "Qty": o["quantity"], "Limit": o["limit_price"],
                            "Status": o["status"], "Fill": o.get("fill_price"), "Fingerprint": o["fingerprint"]} for o in S["orders"]])
        st.dataframe(df.iloc[::-1], use_container_width=True, hide_index=True)
    else:
        st.caption("No orders yet.")
    st.subheader("All proposals")
    if S["pending_proposals"]:
        df = pd.DataFrame([{"Created": p["created_at_utc"][:16], "Ticker": p["symbol"], "Qty": p["quantity"], "Limit": p["limit_price"], "Stop": p["stop_price"],
                            "T1": p["target_1"], "T2": p["target_2"], "Score": p["score"], "R:R": p["expected_rr"], "Status": p["status"]} for p in S["pending_proposals"]])
        st.dataframe(df.iloc[::-1], use_container_width=True, hide_index=True)


def page_watchlist():
    st.title("Watchlist")
    st.caption("Names that scored in the 65–74 band or failed one hard gate. Nothing fires automatically — each needs its trigger and a fresh run.")
    if not S["watchlist"]:
        st.info("Empty. Run the pipeline.")
        return
    df = pd.DataFrame([{"Ticker": w["symbol"], "Setup": w["setup"], "Price": w["price"], "Score": w["score"], "Stop": w["stop"], "T1": w["t1"], "T2": w["t2"],
                        "R:R": w["expected_rr"], "Trigger": w["trigger"], "Why not yet": "; ".join(w["reasons"]), "Added": w["added_at_utc"][:16]} for w in S["watchlist"]])
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_log():
    st.title("Decision log")
    st.caption("Every trade taken, skipped, or exited, with the instruction it was judged against.")
    f1, f2, f3 = st.columns(3)
    decs = sorted({e["decision"] for e in S["decision_log"]})
    sel = f1.multiselect("Decision", decs, default=[])
    sym = f2.text_input("Ticker filter").upper().strip()
    n = f3.slider("Rows", 20, 500, 100)
    entries = [e for e in S["decision_log"] if (not sel or e["decision"] in sel) and (not sym or (e.get("symbol") or "") == sym)]
    _log_table(entries[-n:][::-1])
    md = "# WSO Decision Log\n\n| Time (UTC) | Ticker | Decision | Score | Reason | Rule |\n|---|---|---|---|---|---|\n" + "\n".join(
        f"| {e['at']} | {e.get('symbol') or '—'} | {e['decision']} | {e.get('score', '')} | {e['reason'].replace('|', '/')} | {e['rule']} |" for e in entries)
    st.download_button("Download log (Markdown)", md, file_name="wso_decision_log.md")
    st.download_button("Download log (JSON)", json.dumps(entries, indent=1, default=str), file_name="wso_decision_log.json")
    st.subheader("Email alerts sent")
    if S["alerts"]:
        st.dataframe(pd.DataFrame([{"Time": a["at"][:16], "Type": a["type"], "To": ", ".join(a.get("to", [])), "Sent": a["result"].get("sent"),
                                    "Error": a["result"].get("error", "")} for a in S["alerts"][-50:]][::-1]), use_container_width=True, hide_index=True)


def page_reports():
    st.title("Performance reports")
    kind = st.radio("Report", ["daily", "weekly"], horizontal=True)
    rep = build_report(S, kind)
    st.markdown(rep["html"], unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    if c1.button(f"Email this {kind} report now", type="primary"):
        r = send_report(S, kind, force=True)
        save_state()
        (st.success if r["delivery"]["sent"] else st.error)(json.dumps(r["delivery"]))
    st.caption("Scheduled: daily at 16:30 ET on trading days, weekly on Friday at 16:45 ET (GitHub Actions workflow).")
    st.subheader("Report history")
    hist = S["reports"][kind][-30:][::-1]
    if hist:
        st.dataframe(pd.DataFrame([{"Generated": h["generated_at_utc"][:16], "Equity": h["performance"]["equity"], "Period P&L": h["performance"]["period_pnl"],
                                    "Trades": h["trades"], "Sent": h["delivery"].get("sent"), "Error": h["delivery"].get("error", "")} for h in hist]),
                     use_container_width=True, hide_index=True)


def page_settings():
    st.title("Settings")
    s = S["settings"]

    st.subheader("Trading capital (paper)")
    st.caption("Setting a new amount flattens the paper book and restarts the simulation from that capital.")
    c1, c2 = st.columns([2, 1])
    cap = c1.number_input("Capital to manage (USD)", min_value=100.0, max_value=10_000_000.0, value=float(s["trading_capital"]), step=100.0)
    if c2.button("Apply capital and reset paper account", type="primary"):
        PaperBroker(S, POL).reset_capital(cap)
        log(S, "INFO", f"paper capital set to ${cap:,.2f}; book reset", "Settings")
        save_state()
        st.success(f"Paper account reset to {fmt_money(cap)}")
        st.rerun()
    st.caption(md(f"Balanced-regime position = {POL['size_pct_balanced']:.0f}% → {fmt_money(cap*POL['size_pct_balanced']/100)} · ") +
               md(f"max loss at 30% stop = {fmt_money(cap*POL['size_pct_balanced']/100*0.30)}"))

    st.subheader("Controls")
    a, b, c, d = st.columns(4)
    ha = a.toggle("Human approval required", value=s["human_approval"], help="ON: every proposal waits for your click. OFF: the engine submits paper orders itself after all gates pass.")
    ne = b.toggle("Email notifications", value=s["notifications_enabled"])
    ar = c.toggle("Scheduler may run the pipeline", value=s.get("auto_run_cycle", True))
    ks = d.toggle("Kill switch (halt new positions)", value=s["kill_switch"])
    if (ha, ne, ar, ks) != (s["human_approval"], s["notifications_enabled"], s.get("auto_run_cycle", True), s["kill_switch"]):
        changes = []
        if ha != s["human_approval"]:
            changes.append(f"human approval {'ON' if ha else 'OFF'}")
        if ne != s["notifications_enabled"]:
            changes.append(f"notifications {'ON' if ne else 'OFF'}")
        if ar != s.get("auto_run_cycle", True):
            changes.append(f"scheduler cycle {'ON' if ar else 'OFF'}")
        if ks != s["kill_switch"]:
            changes.append(f"kill switch {'ON' if ks else 'OFF'}")
        s.update(human_approval=ha, notifications_enabled=ne, auto_run_cycle=ar, kill_switch=ks)
        log(S, "INFO", "settings changed: " + ", ".join(changes), "Settings")
        save_state()
        st.toast("Saved: " + ", ".join(changes))

    st.subheader("Email recipients")
    ts = transport_status()
    if ts["configured"]:
        st.markdown(pill(f"Transport: {ts['transport']} · from {ts['from']}", "on"), unsafe_allow_html=True)
    else:
        st.markdown(pill("Email transport not configured", "off") + " <span class='wso-muted'>Add RESEND_API_KEY or SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS "
                    "(+ EMAIL_FROM) to the app secrets. Recipients are saved either way.</span>", unsafe_allow_html=True)
    e1, e2 = st.columns([3, 1])
    new_mail = e1.text_input("Add an email address", placeholder="you@example.com")
    if e2.button("Add", use_container_width=True):
        if valid_email(new_mail) and new_mail.strip().lower() not in [x.lower() for x in s["emails"]]:
            s["emails"].append(new_mail.strip())
            log(S, "INFO", f"notification recipient added: {new_mail.strip()}", "Settings")
            save_state()
            st.rerun()
        else:
            st.error("Enter a valid, not-yet-added address.")
    for m in list(s["emails"]):
        r1, r2 = st.columns([4, 1])
        r1.write(m)
        if r2.button("Remove", key=f"rm_{m}"):
            s["emails"].remove(m)
            log(S, "INFO", f"notification recipient removed: {m}", "Settings")
            save_state()
            st.rerun()
    if s["emails"] and st.button("Send test email"):
        r = send_email(s["emails"], "[WSO] Test message", wrap("Test message", "<p>Email delivery from the WSO trading agent is working.</p>"))
        (st.success if r["sent"] else st.error)(json.dumps(r))
    st.caption("Alerts cover: possible entries (proposals), watchlist additions, fills, target-1 partials, target-2 exits, stops, time stops, "
               "exits before earnings, plus the daily and weekly performance reports.")

    st.subheader("Universe")
    u1, u2 = st.columns(2)
    extra = u1.text_input("Extra tickers (comma-separated)", value=", ".join(s.get("extra_symbols", [])))
    excl = u2.text_input("Excluded tickers (comma-separated)", value=", ".join(s.get("excluded_symbols", [])))
    if st.button("Save universe changes"):
        s["extra_symbols"] = [x.strip().upper() for x in extra.split(",") if x.strip()]
        s["excluded_symbols"] = [x.strip().upper() for x in excl.split(",") if x.strip()]
        save_state()
        st.success("Saved.")

    st.subheader("Risk policy (bounded by your original instructions)")
    with st.form("policy"):
        ov = dict(s.get("policy_overrides", {}))
        cols = st.columns(3)
        fields = [("size_pct_aggressive_growth", "Size % aggressive"), ("size_pct_growth", "Size % growth"), ("size_pct_balanced", "Size % balanced"),
                  ("size_pct_defensive", "Size % defensive"), ("stop_pct_of_position_max", "Max stop % of position"), ("hard_cap_loss_pct_of_account", "Hard cap loss % of account"),
                  ("max_portfolio_heat_pct", "Max portfolio heat %"), ("max_gross_exposure_pct", "Max gross exposure %"), ("max_positions", "Max positions"),
                  ("max_positions_per_sector", "Max per sector"), ("daily_loss_limit_pct", "Daily loss breaker %"), ("weekly_loss_limit_pct", "Weekly loss breaker %"),
                  ("consecutive_losses_pause", "Pause after N losses"), ("min_score_to_propose", "Min WSO score"), ("min_reward_risk", "Min reward:risk"),
                  ("setup_window_trading_days", "Setup window (days)"), ("max_hold_trading_days", "Max hold (days)")]
        vals = {}
        for i, (k, label) in enumerate(fields):
            lo, hi = P.BOUNDS.get(k, (0, 1e9))
            cur = POL[k]
            vals[k] = cols[i % 3].number_input(f"{label} [{lo}–{hi}]", min_value=float(lo), max_value=float(hi), value=float(cur), step=1.0 if isinstance(cur, int) else 0.5)
        if st.form_submit_button("Save policy"):
            for k, v in vals.items():
                v = int(v) if isinstance(P.FLAT_DEFAULTS[k], int) else float(v)
                if v != P.FLAT_DEFAULTS[k]:
                    ov[k] = v
                else:
                    ov.pop(k, None)
            s["policy_overrides"] = ov
            log(S, "INFO", f"policy overrides saved: {ov}", "Settings")
            save_state()
            st.success("Policy saved (bounds enforced).")
            st.rerun()
    st.caption(f"Persistence: {_store().describe()}")


def page_rules():
    st.title("Rules in force")
    st.markdown("These are the binding rules the engine applies on every run. The full source document is in `docs/`.")
    for line in P.summary_lines(POL):
        st.markdown("- " + line)
    st.markdown("""
**Decision vocabulary**: APPROVE, APPROVE_WITH_REDUCED_SIZE, WATCHLIST, REJECT, EXIT, REDUCE, HOLD, TAKE_PARTIAL_PROFIT.
Default under uncertainty is REJECT or WATCHLIST.

**Score bands**: < 65 reject · 65–74 watchlist · 75–81 conditional (half size) · 82–89 strong · 90+ exceptional.

**Exit rules (paper monitor)**: hard stop at invalidation; half off at T1 and stop to breakeven; full exit at T2; stop trails to +1R once +2R;
time stop if no progress within the setup window; exit the session before an earnings report; never widen a stop, never average down.

**Labels**: every number on the Run Pipeline page is a *fact* computed from vendor data with its as-of date; the regime label, setup name,
score and decision are *rule-based inferences*. Missing vendor fields are shown as None — never estimated.
""")
    with st.expander("Scoring weights"):
        st.table(pd.DataFrame({"factor": list(WEIGHTS), "weight": list(WEIGHTS.values())}))


pages = [st.Page(page_dashboard, title="Dashboard", icon="🏠", default=True),
         st.Page(page_pipeline, title="Run pipeline", icon="⚙️"),
         st.Page(page_positions, title="Positions & orders", icon="📊"),
         st.Page(page_watchlist, title="Watchlist", icon="👀"),
         st.Page(page_log, title="Decision log", icon="📝"),
         st.Page(page_reports, title="Reports", icon="📬"),
         st.Page(page_settings, title="Settings", icon="🛠️"),
         st.Page(page_rules, title="Rules", icon="📜")]
if __import__("os").environ.get("WSO_PAGE"):   # test harness: render one page directly
    {"dashboard": page_dashboard, "pipeline": page_pipeline, "positions": page_positions, "watchlist": page_watchlist,
     "log": page_log, "reports": page_reports, "settings": page_settings, "rules": page_rules}[__import__("os").environ["WSO_PAGE"]]()
else:
    st.navigation(pages).run()
