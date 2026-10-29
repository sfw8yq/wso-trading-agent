"""Gate 6 — deterministic risk engine. Pure arithmetic, no judgement.

Every check records its numbers so the decision log can show exactly why an
order passed or failed. The same function guards paper and (future) live
orders.
"""
from __future__ import annotations

import datetime as dt
import hashlib

from . import policy as P
from .universe import cluster_of, sector_of


def fingerprint(p: dict) -> str:
    key = "|".join(str(p.get(k, "")) for k in ["account", "symbol", "instrument", "side", "quantity", "limit_price", "strategy"])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def account_view(state: dict, quotes: dict[str, float] | None = None) -> dict:
    """Derive the account snapshot the engine needs from state."""
    acct, positions = state["account"], state["positions"]
    quotes = quotes or {}
    mv = sum(p["quantity"] * quotes.get(p["symbol"], p.get("last_price", p["entry_price"])) for p in positions)
    equity = acct["cash"] + mv
    open_risk = sum(max(0.0, (quotes.get(p["symbol"], p.get("last_price", p["entry_price"])) - p["stop"]) * p["quantity"]) for p in positions)
    today = dt.date.today()
    closed = state["trades"]
    daily_pnl = sum(t["pnl"] for t in closed if t["exit_date"] == str(today))
    week_start = today - dt.timedelta(days=today.weekday())
    weekly_pnl = sum(t["pnl"] for t in closed if t["exit_date"] >= str(week_start))
    # unrealized counts toward breakers too
    unreal = sum((quotes.get(p["symbol"], p.get("last_price", p["entry_price"])) - p["entry_price"]) * p["quantity"] for p in positions)
    consec = 0
    for t in reversed(closed):
        if t["pnl"] < 0:
            consec += 1
        else:
            break
    return {"cash": acct["cash"], "buying_power": acct["cash"], "equity": equity, "market_value": mv,
            "open_risk_dollars": open_risk, "gross_exposure_dollars": mv, "daily_pnl": daily_pnl + min(0.0, unreal),
            "weekly_pnl": weekly_pnl + min(0.0, unreal), "consecutive_losses": consec,
            "open_positions": len(positions), "snapshot_timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def evaluate(proposal: dict, state: dict, policy: dict, acct: dict | None = None, pre_check: bool = False) -> dict:
    """Return {'approved': bool, 'checks': [(PASS/FAIL, text)], 'fingerprint': str}."""
    acct = acct or account_view(state)
    settings = state["settings"]
    checks: list[tuple[str, str]] = []
    allpass = True

    def ok(cond: bool, msg: str) -> bool:
        nonlocal allpass
        checks.append(("PASS" if cond else "FAIL", msg))
        allpass &= bool(cond)
        return bool(cond)

    equity = acct["equity"]
    bp = acct["buying_power"]
    price = float(proposal["limit_price"])
    qty = float(proposal["quantity"])
    pos_val = price * qty
    regime = proposal["market_regime"]
    allowed_pct = P.size_pct_for_regime(policy, regime, proposal.get("size_step_down", 0))
    if proposal.get("decision") == "APPROVE_WITH_REDUCED_SIZE":
        allowed_pct = allowed_pct / 2

    # kill switch & breakers
    ok(not settings.get("kill_switch"), "kill switch inactive")
    ok(acct["daily_pnl"] > -equity * policy["daily_loss_limit_pct"] / 100,
       f"daily P&L ${acct['daily_pnl']:,.2f} above −{policy['daily_loss_limit_pct']}% breaker")
    ok(acct["weekly_pnl"] > -equity * policy["weekly_loss_limit_pct"] / 100,
       f"weekly P&L ${acct['weekly_pnl']:,.2f} above −{policy['weekly_loss_limit_pct']}% breaker")
    ok(acct["consecutive_losses"] < policy["consecutive_losses_pause"],
       f"consecutive losses {acct['consecutive_losses']} < {policy['consecutive_losses_pause']}")

    # approval
    if settings.get("human_approval", True):
        ok(proposal.get("user_approved") is True, "human approval recorded (approval stage ON)")
    else:
        checks.append(("PASS", "human approval stage OFF (auto mode)"))

    # freshness
    now = dt.datetime.now(dt.timezone.utc)
    try:
        qage = (now - dt.datetime.fromisoformat(proposal["quote_timestamp_utc"])).total_seconds()
    except Exception:
        qage = 1e9
    ok(qage <= policy["quote_max_age_seconds"], f"quote age {qage:.0f}s ≤ {policy['quote_max_age_seconds']}s")
    try:
        aage = (now - dt.datetime.fromisoformat(acct["snapshot_timestamp_utc"])).total_seconds()
    except Exception:
        aage = 1e9
    ok(aage <= policy["account_max_age_seconds"], f"account snapshot age {aage:.0f}s ≤ {policy['account_max_age_seconds']}s")
    if policy.get("trading_hours_only"):
        from .data import is_market_open
        if pre_check:
            checks.append(("INFO", "trading-hours check deferred to submission"))
        else:
            ok(is_market_open(), "regular trading hours (orders submit 9:30–16:00 ET only)")

    # buying power & size
    ok(bp > 0, f"buying power ${bp:,.2f} > 0")
    ok(pos_val <= bp + 1e-6, f"position ${pos_val:,.2f} ≤ buying power ${bp:,.2f}")
    ok(regime != "risk_off", f"regime {regime} permits new positions")
    size_pct = 100 * pos_val / equity if equity else 999
    ok(size_pct <= allowed_pct + 1e-6, f"size {size_pct:.2f}% of equity ≤ {allowed_pct:.1f}% ({regime}{' reduced' if proposal.get('decision')=='APPROVE_WITH_REDUCED_SIZE' else ''})")
    ok(size_pct <= policy["size_pct_hard_max"] + 1e-6, f"size {size_pct:.2f}% ≤ {policy['size_pct_hard_max']}% hard max")

    # per-trade loss (per POSITION, user-confirmed)
    stop = float(proposal["stop_price"])
    stop_pct = 100 * (price - stop) / price
    max_loss = pos_val * stop_pct / 100
    ok(stop_pct > 0, f"stop {stop} is below entry {price}")
    ok(stop_pct <= policy["stop_pct_of_position_max"], f"stop distance {stop_pct:.2f}% ≤ {policy['stop_pct_of_position_max']}% of position")
    ok(100 * max_loss / equity <= policy["hard_cap_loss_pct_of_account"], f"max loss ${max_loss:,.2f} = {100*max_loss/equity:.2f}% of account ≤ {policy['hard_cap_loss_pct_of_account']}%")
    heat = acct["open_risk_dollars"] + max_loss
    ok(100 * heat / equity <= policy["max_portfolio_heat_pct"], f"portfolio heat after ${heat:,.2f} = {100*heat/equity:.2f}% ≤ {policy['max_portfolio_heat_pct']}%")
    gross = acct["gross_exposure_dollars"] + pos_val
    ok(100 * gross / equity <= policy["max_gross_exposure_pct"], f"gross exposure after {100*gross/equity:.1f}% ≤ {policy['max_gross_exposure_pct']}%")
    ok(100 * (acct["cash"] - pos_val) / equity >= policy["min_cash_reserve_pct"] - 1e-6, f"cash reserve after {100*(acct['cash']-pos_val)/equity:.1f}% ≥ {policy['min_cash_reserve_pct']}%")

    # counts & sectors
    sym = proposal["symbol"]
    sec, cl = sector_of(sym), cluster_of(sym)
    in_sector = sum(1 for p in state["positions"] if sector_of(p["symbol"]) == sec or (cl and cluster_of(p["symbol"]) == cl))
    ok(acct["open_positions"] < policy["max_positions"], f"open positions {acct['open_positions']} < {policy['max_positions']}")
    ok(in_sector < policy["max_positions_per_sector"], f"positions in sector/cluster {in_sector} < {policy['max_positions_per_sector']}")
    sector_val = sum(p["quantity"] * p.get("last_price", p["entry_price"]) for p in state["positions"] if sector_of(p["symbol"]) == sec) + pos_val
    ok(100 * sector_val / equity <= policy["max_sector_exposure_pct"], f"sector exposure after {100*sector_val/equity:.1f}% ≤ {policy['max_sector_exposure_pct']}%")
    ok(not any(p["symbol"] == sym for p in state["positions"]), f"no existing position in {sym} (never add/average)")

    # quality gates
    ok(float(proposal["score"]) >= policy["min_score_to_propose"], f"WSO score {proposal['score']} ≥ {policy['min_score_to_propose']}")
    ok(float(proposal["expected_rr"]) >= policy["min_reward_risk"], f"reward:risk {proposal['expected_rr']} ≥ {policy['min_reward_risk']}")
    ok(proposal.get("order_type") == "limit", "order type is limit")
    ok(proposal.get("earnings_inside_window") is False, "no earnings inside holding window")
    ref = float(proposal.get("reference_price") or price)
    ok(price <= ref * (1 + policy["equity_limit_band_pct"] / 100) + 1e-9, f"limit {price} ≤ reference {ref} × (1+{policy['equity_limit_band_pct']}%)")
    ok(abs(price / ref - 1) <= 0.01, "price moved ≤ 1% from analysis quote")

    # duplicates
    fp = fingerprint(proposal)
    ok(fp not in state.get("fingerprints", []), f"fingerprint {fp} not a duplicate")
    return {"approved": bool(allpass), "checks": checks, "fingerprint": fp,
            "max_loss": round(max_loss, 2), "position_value": round(pos_val, 2), "size_pct": round(size_pct, 2),
            "evaluated_at_utc": now.isoformat(timespec="seconds")}


def size_proposal(tech: dict, decision: dict, regime: str, acct: dict, policy: dict, quote: dict) -> dict:
    """Build an immutable execution packet sized by policy."""
    equity = acct["equity"]
    step = 1 if tech["atr_pct"] > policy["high_vol_atr_pct_step_down"] else 0
    pct = P.size_pct_for_regime(policy, regime, step)
    if decision["decision"] == "APPROVE_WITH_REDUCED_SIZE":
        pct /= 2
    alloc = equity * pct / 100
    # respect cash reserve and buying power
    alloc = min(alloc, acct["cash"] - equity * policy["min_cash_reserve_pct"] / 100, acct["cash"])
    ref = float(quote["price"])
    limit = round(ref * (1 + policy["equity_limit_band_pct"] / 200), 2)   # half the band above last
    qty = alloc / limit if alloc > 0 else 0
    qty = round(qty, 4) if policy["paper_fractional_shares"] else float(int(qty))
    # floating-point guard: never exceed the allocation
    while qty > 0 and qty * limit > alloc + 1e-9:
        qty = round(qty - (0.0001 if policy["paper_fractional_shares"] else 1), 4)
    return {"account": "PAPER", "symbol": tech["symbol"], "instrument": "equity", "side": "buy", "strategy": tech["setup"],
            "quantity": qty, "limit_price": limit, "reference_price": ref, "stop_price": tech["stop"],
            "target_1": tech["t1"], "target_2": tech["t2"], "expected_rr": tech["expected_rr"], "score": decision["score"],
            "decision": decision["decision"], "market_regime": regime, "size_step_down": step, "size_pct_target": pct,
            "order_type": "limit", "time_in_force": "day", "earnings_inside_window": False,
            "quote_timestamp_utc": quote["timestamp_utc"], "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "user_approved": False, "status": "pending_approval", "max_hold_trading_days": policy["max_hold_trading_days"],
            "setup_window_trading_days": policy["setup_window_trading_days"]}
