"""Orchestrator — runs the 10-gate WSO process end to end.

Gate 1 account validation → Gate 2 regime → Gate 3 universe/screen →
Gate 4 specialists (technical, fundamentals, catalyst) → Gate 5 Critic →
Gate 6 deterministic risk engine → Gate 7 data integrity (freshness) →
Gate 8 broker eligibility (paper) → Gate 9 pre-trade review (human approval
when ON) → Gate 10 duplicate / kill-switch.
"""
from __future__ import annotations

import datetime as dt
import traceback
import uuid

from . import policy as P
from .data import download_history, fundamentals, latest_quote, next_earnings_date, now_utc, trading_days_between
from .notify import event_html, send_email, setup_alert_html
from .paper_broker import PaperBroker
from .regime import compute_regime
from .risk_engine import account_view, evaluate, size_proposal
from .scoring import critic, decide, factor_scores, penalties, wso_score
from .technical import analyze, apply_analyst_target
from .universe import REGIME_SYMBOLS, all_symbols, sector_of

MAX_NEW_PER_CYCLE = {"aggressive_growth": 3, "growth": 2, "balanced": 2, "defensive": 1, "risk_off": 0}


def log(state: dict, decision: str, reason: str, rule: str, symbol: str | None = None, **extra) -> dict:
    e = {"id": uuid.uuid4().hex[:8], "at": now_utc().isoformat(timespec="seconds"), "symbol": symbol,
         "decision": decision, "reason": reason, "rule": rule, **extra}
    state["decision_log"].append(e)
    state["decision_log"] = state["decision_log"][-2000:]
    return e


def _alert(state: dict, subject: str, html: str, kind: str) -> dict:
    s = state["settings"]
    if not s["notifications_enabled"] or not s["emails"]:
        res = {"sent": False, "error": "notifications off or no recipients"}
    else:
        res = send_email(s["emails"], subject, html)
    state["alerts"].append({"at": now_utc().isoformat(timespec="seconds"), "type": kind, "subject": subject, "to": list(s["emails"]), "result": res})
    state["alerts"] = state["alerts"][-500:]
    return res


def run_cycle(state: dict, progress=None, shortlist_size: int = 18) -> dict:
    """Full pipeline. Mutates and returns a summary (caller saves state)."""
    pol = P.merged(state["settings"].get("policy_overrides"))
    s = state["settings"]
    summary = {"started_at_utc": now_utc().isoformat(timespec="seconds"), "proposals": [], "watchlist": [], "rejected": [], "errors": []}

    def step(msg):
        if progress:
            progress(msg)

    # Gate 1 — account / kill switch
    acct = account_view(state)
    if s.get("kill_switch"):
        log(state, "HOLD", "kill switch active: no new positions", "Kill switch / Gate 10")
        summary["halted"] = "kill_switch"
        return summary
    if acct["buying_power"] <= 0:
        log(state, "REJECT", "buying power is zero", "Gate 8 buying power")
        summary["halted"] = "no_buying_power"
        return summary

    # Gate 2 — regime (with universe breadth)
    step("Downloading price history (universe + indices)…")
    syms = all_symbols(s.get("extra_symbols"), s.get("excluded_symbols"))
    hist = download_history(REGIME_SYMBOLS + syms, period="1y")
    uni_hist = {k: v for k, v in hist.items() if k in syms}
    step("Computing market regime…")
    regime = compute_regime(hist, uni_hist)
    state["regime"] = regime
    log(state, "INFO", f"regime {regime['regime']} (score {regime['score']}); leaders {', '.join(regime['leaders'])}; "
        f"laggards {', '.join(regime['laggards'])}", "Gate 2 market regime")
    if not regime["new_positions_allowed"]:
        log(state, "HOLD", "risk_off regime: no new positions permitted", "Position sizing: risk_off = 0%")
        summary["regime"] = regime
        summary["halted"] = "risk_off"
        return summary
    spy = hist.get("SPY")

    # Gate 3 — screen on technical structure first (cheap), then enrich the shortlist
    step(f"Screening {len(uni_hist)} names…")
    pre = []
    for sym, df in uni_hist.items():
        try:
            t = analyze(sym, df, spy)
        except Exception:
            continue
        if t["avg_dollar_volume"] < pol["list_b_min_avg_dollar_volume"] or t["price"] < pol["min_share_price"]:
            continue
        if t["setup"] in ("none", "pullback_below_21ema"):
            continue
        if t["pct_from_52w_high"] < -pol["max_distance_from_52w_high_pct"] - 5:
            continue
        pre.append(t)
    # rank by relative strength and proximity to highs, keep leaders-sector names first
    pre.sort(key=lambda t: (sector_of(t["symbol"]) in regime["leaders"], t["rs_3m_pct"] + t["rs_1m_pct"]), reverse=True)
    shortlist = pre[:shortlist_size]
    log(state, "INFO", f"{len(pre)} names passed the structural screen; {len(shortlist)} shortlisted for specialist review",
        "Gate 3 universe screen")

    # Gate 4/5 — specialists + critic per name
    today = dt.date.today()
    window_days = int(pol["setup_window_trading_days"])
    evaluated = []
    for i, t0 in enumerate(shortlist):
        sym = t0["symbol"]
        step(f"Specialists: {sym} ({i+1}/{len(shortlist)})…")
        try:
            nxt, last, surprise = next_earnings_date(sym)
            fund = fundamentals(sym)
            t = analyze(sym, uni_hist[sym], spy, last_earnings=last)
            t = apply_analyst_target(t, fund.get("targetMedianPrice"))
            t["data_quality"] = fund.get("data_quality", "ok")
            inside = bool(nxt and trading_days_between(today, nxt) <= window_days)
            earn = {"next": str(nxt) if nxt else None, "last": str(last) if last else None, "surprise": surprise,
                    "inside_window": inside, "days_to_next": trading_days_between(today, nxt) if nxt else None}
            f, notes = factor_scores(t, fund, regime, earn, pol)
            pen = penalties(t, earn, regime, pol, state["positions"], today)
            score = wso_score(f, pen)
            bear = critic(t, f, pen, earn, regime)
            decision, reasons = decide(score, t, earn, regime, pol)
            if fund.get("marketCap") and fund["marketCap"] < pol["list_b_min_market_cap"]:
                decision, reasons = "REJECT", reasons + ["market cap below List B minimum"]
            rec = {"symbol": sym, "name": fund.get("longName"), "sector": sector_of(sym), "price": t["price"], "setup": t["setup"],
                   "score": score, "factors": f, "penalties": pen, "notes": notes, "bear_case": bear, "decision": decision,
                   "reasons": reasons, "tech": t, "earnings": earn, "expected_rr": t["expected_rr"], "stop": t["stop"],
                   "stop_pct": t["stop_pct"], "t1": t["t1"], "t2": t["t2"], "data_quality": t["data_quality"],
                   "fund": {k: fund.get(k) for k in ("marketCap", "forwardPE", "revenueGrowth", "profitMargins", "debtToEquity",
                                                     "targetMedianPrice", "recommendationMean", "heldPercentInstitutions")},
                   "evaluated_at_utc": now_utc().isoformat(timespec="seconds")}
            evaluated.append(rec)
            log(state, decision, "; ".join(reasons) or "all gates clear", "WSO score + CIO decision rules", symbol=sym,
                score=score, setup=t["setup"], expected_rr=t["expected_rr"], stop=t["stop"], t1=t["t1"], t2=t["t2"],
                earnings_next=earn["next"], bear_case=bear[:3])
        except Exception as e:
            summary["errors"].append(f"{sym}: {e}")
            log(state, "REJECT", f"data error: {e}", "Data integrity: never invent values", symbol=sym)

    evaluated.sort(key=lambda r: r["score"], reverse=True)
    state["last_candidates"] = evaluated

    # Watchlist (keep newest evaluation per symbol)
    new_watch = [{"symbol": r["symbol"], "setup": r["setup"], "price": r["price"], "score": r["score"], "reasons": r["reasons"],
                  "stop": r["stop"], "t1": r["t1"], "t2": r["t2"], "expected_rr": r["expected_rr"], "added_at_utc": r["evaluated_at_utc"],
                  "trigger": _trigger_text(r)} for r in evaluated if r["decision"] == "WATCHLIST"]
    prev_syms = {w["symbol"] for w in state.get("watchlist", [])}
    state["watchlist"] = new_watch
    summary["watchlist"] = new_watch
    summary["rejected"] = [r["symbol"] for r in evaluated if r["decision"] == "REJECT"]

    # Gate 6–10 — size, risk-engine, approval, paper order
    broker = PaperBroker(state, pol)
    approved = [r for r in evaluated if r["decision"] in ("APPROVE", "APPROVE_WITH_REDUCED_SIZE")]
    max_new = min(MAX_NEW_PER_CYCLE[regime["regime"]], pol["max_positions"] - len(state["positions"]))
    pending_syms = {p["symbol"] for p in state["pending_proposals"] if p["status"] == "pending_approval"}
    taken_sectors = [sector_of(p["symbol"]) for p in state["positions"]]
    proposals = []
    for r in approved:
        if len(proposals) >= max_new:
            log(state, "WATCHLIST", f"max new positions this cycle ({max_new}) reached", "Regime size schedule", symbol=r["symbol"])
            continue
        if r["symbol"] in pending_syms or any(p["symbol"] == r["symbol"] for p in state["positions"]):
            continue
        if taken_sectors.count(r["sector"]) >= pol["max_positions_per_sector"]:
            log(state, "REJECT", "sector limit reached", "Portfolio limits: max positions per sector", symbol=r["symbol"])
            continue
        step(f"Sizing and risk engine: {r['symbol']}…")
        q = latest_quote(r["symbol"])
        if not q.get("price"):
            log(state, "REJECT", "no fresh quote", "Data integrity", symbol=r["symbol"])
            continue
        acct = account_view(state)
        packet = size_proposal(r["tech"], r, regime["regime"], acct, pol, q)
        packet["id"] = uuid.uuid4().hex[:8]
        packet["bear_case"], packet["notes"] = r["bear_case"], r["notes"]
        if packet["quantity"] <= 0:
            log(state, "REJECT", "allocation after cash reserve is zero", "Portfolio limits: cash reserve", symbol=r["symbol"])
            continue
        pre = evaluate({**packet, "user_approved": True}, state, pol, acct, pre_check=True)   # approval + hours checked at submission
        packet["risk_checks"] = pre["checks"]
        packet["max_loss"], packet["position_value"], packet["size_pct"] = pre["max_loss"], pre["position_value"], pre["size_pct"]
        if not pre["approved"]:
            fails = [m for st_, m in pre["checks"] if st_ == "FAIL"]
            log(state, "REJECT", "risk engine: " + "; ".join(fails), "Gate 6 deterministic risk engine", symbol=r["symbol"])
            continue
        taken_sectors.append(r["sector"])
        if s.get("human_approval", True):
            packet["status"] = "pending_approval"
            state["pending_proposals"].append(packet)
            log(state, "PROPOSED", f"awaiting human approval: {packet['quantity']} sh @ ≤{packet['limit_price']} stop {packet['stop_price']} "
                f"T1 {packet['target_1']} T2 {packet['target_2']} (max loss ${packet['max_loss']})", "Gate 9 user approval stage ON",
                symbol=r["symbol"], score=r["score"])
        else:
            packet["user_approved"] = True
            packet["status"] = "auto_approved"
            final = evaluate(packet, state, pol, account_view(state))
            if final["approved"]:
                order = broker.place_limit_buy(packet, final["fingerprint"])
                packet["status"] = order["status"]
                packet["order_id"] = order["id"]
                log(state, "APPROVE", f"paper limit order {order['status']}: {packet['quantity']} sh @ ≤{packet['limit_price']}",
                    "Gate 9 user approval stage OFF → auto", symbol=r["symbol"], score=r["score"], order_id=order["id"])
                if order["status"] == "filled":
                    pos = state["positions"][-1]
                    title, html = event_html({"type": "entry_filled", "symbol": r["symbol"], "position": pos})
                    _alert(state, f"[WSO] {title}", html, "entry_filled")
            else:
                fails = [m for st_, m in final["checks"] if st_ == "FAIL"]
                log(state, "REJECT", "final risk engine: " + "; ".join(fails), "Gate 6", symbol=r["symbol"])
                continue
        proposals.append(packet)

    summary["proposals"] = proposals
    summary["regime"] = regime
    summary["evaluated"] = len(evaluated)
    degraded = sum(1 for r in evaluated if r.get("data_quality") == "degraded")
    summary["data_quality"] = "degraded" if evaluated and degraded > len(evaluated) / 2 else "ok"
    if summary["data_quality"] == "degraded":
        log(state, "HOLD", f"vendor fundamentals unavailable for {degraded}/{len(evaluated)} names — scores reduced, decisions capped at WATCHLIST",
            "Never invent values; default under uncertainty = WATCHLIST")
    summary["finished_at_utc"] = now_utc().isoformat(timespec="seconds")
    state["last_cycle"] = {k: v for k, v in summary.items() if k not in ("proposals",)} | {"proposal_symbols": [p["symbol"] for p in proposals]}

    # Alerts: possible entries and new watchlist names
    new_names = [w for w in new_watch if w["symbol"] not in prev_syms]
    if proposals or new_names:
        html = setup_alert_html(proposals, new_watch, regime, s.get("human_approval", True))
        subj = f"[WSO] Setup alert: {len(proposals)} possible entr{'y' if len(proposals)==1 else 'ies'}, {len(new_watch)} on watchlist"
        _alert(state, subj, html, "setup_alert")
    if not proposals:
        log(state, "HOLD", "no candidate cleared score ≥ 75 and R:R ≥ 2.0 — no new orders", "Default under uncertainty = WATCHLIST")
    return summary


def _trigger_text(r: dict) -> str:
    t = r["tech"]
    if t["setup"] in ("breakout_watch", "new_52w_high"):
        return f"close above {t['hi52']} on ≥1.2× volume, or pullback to 21 EMA {t['ema21']} that holds"
    if t["setup"] == "post_earnings_continuation":
        return f"hold above reaction low {t['post_earnings']['reaction_low'] if t.get('post_earnings') else t['low10']} and clear {t['t1']}"
    if t["setup"] == "extended":
        return f"pullback toward 21 EMA {t['ema21']} (currently {t['ext_vs_21ema_pct']:.1f}% above)"
    return f"re-test of 21 EMA {t['ema21']} with rising volume"


# -------------------------------------------------------------- approvals
def approve_proposal(state: dict, proposal_id: str, approver: str = "site") -> dict:
    pol = P.merged(state["settings"].get("policy_overrides"))
    pk = next((p for p in state["pending_proposals"] if p["id"] == proposal_id), None)
    if not pk or pk["status"] != "pending_approval":
        return {"ok": False, "error": "proposal not found or not pending"}
    q = latest_quote(pk["symbol"])
    if not q.get("price"):
        return {"ok": False, "error": "no fresh quote"}
    # Packet is immutable: we only refresh the quote timestamp and verify the price band.
    pk["quote_timestamp_utc"] = q["timestamp_utc"]
    moved = abs(q["price"] / pk["reference_price"] - 1)
    if moved > 0.01:
        pk["status"] = "not_submitted_price_moved"
        log(state, "REJECT", f"price moved {100*moved:.2f}% from analysis quote — return to analysis", "Execution: price outside band → not_submitted", symbol=pk["symbol"])
        return {"ok": False, "error": f"price moved {100*moved:.2f}% since analysis; proposal voided"}
    pk["user_approved"], pk["approved_by"], pk["approved_at_utc"] = True, approver, now_utc().isoformat(timespec="seconds")
    res = evaluate(pk, state, pol, account_view(state))
    pk["risk_checks"] = res["checks"]
    if not res["approved"]:
        pk["status"] = "rejected_by_engine"
        fails = [m for s_, m in res["checks"] if s_ == "FAIL"]
        log(state, "REJECT", "risk engine after approval: " + "; ".join(fails), "Gate 6", symbol=pk["symbol"])
        return {"ok": False, "error": "; ".join(fails)}
    broker = PaperBroker(state, pol)
    order = broker.place_limit_buy(pk, res["fingerprint"])
    pk["status"], pk["order_id"] = order["status"], order["id"]
    log(state, "APPROVE", f"user approved; paper limit order {order['status']} {pk['quantity']} sh @ ≤{pk['limit_price']}", "Gate 9 user approval", symbol=pk["symbol"], order_id=order["id"])
    if order["status"] == "filled":
        pos = state["positions"][-1]
        title, html = event_html({"type": "entry_filled", "symbol": pk["symbol"], "position": pos})
        _alert(state, f"[WSO] {title}", html, "entry_filled")
    return {"ok": True, "order": order}


def reject_proposal(state: dict, proposal_id: str, note: str = "") -> dict:
    pk = next((p for p in state["pending_proposals"] if p["id"] == proposal_id), None)
    if not pk:
        return {"ok": False}
    pk["status"] = "rejected_by_user"
    log(state, "REJECT", f"user declined{': ' + note if note else ''}", "Gate 9 user approval", symbol=pk["symbol"])
    return {"ok": True}


def expire_proposals(state: dict) -> int:
    n = 0
    today = str(dt.date.today())
    for p in state["pending_proposals"]:
        if p["status"] == "pending_approval" and p["created_at_utc"][:10] < today:
            p["status"] = "expired"
            n += 1
            log(state, "WATCHLIST", "proposal expired unexecuted (day order discipline)", "Execution: day time-in-force", symbol=p["symbol"])
    state["pending_proposals"] = [p for p in state["pending_proposals"] if p["status"] == "pending_approval"] + \
                                 [p for p in state["pending_proposals"] if p["status"] != "pending_approval"][-50:]
    return n


# ---------------------------------------------------------------- monitor
def monitor(state: dict) -> dict:
    pol = P.merged(state["settings"].get("policy_overrides"))
    broker = PaperBroker(state, pol)
    out = {"filled": [], "events": [], "cancelled": 0, "expired": expire_proposals(state)}
    try:
        for o in broker.try_fill_open_orders():
            out["filled"].append(o["symbol"])
            pos = next((p for p in state["positions"] if p["order_id"] == o["id"]), None)
            log(state, "FILLED", f"paper limit order filled at {o['fill_price']}", "Execution", symbol=o["symbol"])
            if pos:
                title, html = event_html({"type": "entry_filled", "symbol": o["symbol"], "position": pos})
                _alert(state, f"[WSO] {title}", html, "entry_filled")
        for ev in broker.monitor():
            out["events"].append({"type": ev["type"], "symbol": ev["symbol"]})
            dec = {"stop_hit": "EXIT", "target_2_hit": "EXIT", "target_1_partial": "TAKE_PARTIAL_PROFIT", "time_stop": "EXIT",
                   "exit_before_earnings": "EXIT", "stop_trailed": "HOLD"}[ev["type"]]
            tr = ev.get("trade")
            log(state, dec, f"{ev['type']}" + (f": P&L ${tr['pnl']} ({tr['r_multiple']}R)" if tr else f": stop now {ev['position']['stop']}"),
                "Exit rules (stop / targets / time / earnings)", symbol=ev["symbol"])
            title, html = event_html(ev)
            _alert(state, f"[WSO] {title}", html, ev["type"])
        out["cancelled"] = len(broker.cancel_open_orders_eod())
        out["mark"] = broker.mark_to_market()
    except Exception as e:
        out["error"] = f"{e}\n{traceback.format_exc()[-400:]}"
    return out
