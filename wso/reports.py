"""Performance metrics and daily/weekly report assembly."""
from __future__ import annotations

import datetime as dt

from .notify import report_html, send_email
from .store import utcnow


def performance(state: dict, period_start: dt.date) -> dict:
    acct, trades, positions = state["account"], state["trades"], state["positions"]
    mv = sum(p["quantity"] * p.get("last_price", p["entry_price"]) for p in positions)
    equity = acct["cash"] + mv
    start_cap = acct["starting_capital"]
    curve = acct["equity_curve"]
    # period P&L from equity curve (fallback: realized trades in period)
    base = None
    for pt in curve:
        if pt["date"] < str(period_start):
            base = pt["equity"]
    if base is None:
        base = curve[0]["equity"] if curve else start_cap
    period_trades = [t for t in trades if t["exit_date"] >= str(period_start)]
    period_pnl = equity - base
    wins = [t for t in trades if t["pnl"] > 0]
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    peak, mdd = -1e18, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        if peak > 0:
            mdd = max(mdd, 100 * (peak - pt["equity"]) / peak)
    heat = sum(max(0.0, (p.get("last_price", p["entry_price"]) - p["stop"]) * p["quantity"]) for p in positions)
    return {"equity": round(equity, 2), "cash": round(acct["cash"], 2), "market_value": round(mv, 2),
            "period_pnl": round(period_pnl, 2), "period_pnl_pct": round(100 * period_pnl / base, 2) if base else 0.0,
            "total_pnl": round(equity - start_cap, 2), "total_return_pct": round(100 * (equity / start_cap - 1), 2) if start_cap else 0.0,
            "period_trades": len(period_trades), "total_trades": len(trades),
            "win_rate": 100 * len(wins) / len(trades) if trades else 0.0,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "max_drawdown_pct": round(mdd, 2),
            "heat_pct": round(100 * heat / equity, 2) if equity else 0.0, "period_start": str(period_start)}


def build_report(state: dict, kind: str) -> dict:
    today = dt.date.today()
    start = today if kind == "daily" else today - dt.timedelta(days=today.weekday())
    perf = performance(state, start)
    trades = [t for t in state["trades"] if t["exit_date"] >= str(start)]
    decisions = [d for d in state["decision_log"] if d["at"][:10] >= str(start)]
    html = report_html(kind, perf, trades, state["positions"], decisions, state.get("regime"))
    return {"kind": kind, "generated_at_utc": utcnow(), "period_start": str(start), "performance": perf,
            "html": html, "trades": len(trades), "decisions": len(decisions)}


def send_report(state: dict, kind: str, force: bool = False) -> dict:
    rep = build_report(state, kind)
    s = state["settings"]
    result = {"sent": False, "error": "notifications off or no recipients"}
    if (s["notifications_enabled"] or force) and s["emails"]:
        subj = f"[WSO] {'Daily' if kind == 'daily' else 'Weekly'} report — equity ${rep['performance']['equity']:,.2f} ({rep['performance']['period_pnl_pct']:+.2f}%)"
        result = send_email(s["emails"], subj, rep["html"])
    rep["delivery"] = result
    rep_no_html = {k: v for k, v in rep.items() if k != "html"}
    state["reports"][kind].append(rep_no_html)
    state["reports"][kind] = state["reports"][kind][-120:]
    state["alerts"].append({"at": utcnow(), "type": f"{kind}_report", "to": list(s["emails"]), "result": result})
    return rep
