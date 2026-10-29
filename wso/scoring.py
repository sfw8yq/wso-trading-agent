"""WSO 12-factor opportunity score, penalties, Critic bear case and the
decision mapping from the user's document.

Weights (sum 1.00): regime .10, business quality .12, financial strength .10,
growth .10, moat .06, valuation .05, technical .17, catalyst .12,
institutional .06, liquidity .04, reward/risk .08 — minus penalties.
"""
from __future__ import annotations

import datetime as dt

from .technical import technical_score
from .universe import cluster_of, sector_of

WEIGHTS = {"regime": .10, "business_quality": .12, "financial_strength": .10, "growth": .10, "moat": .06,
           "valuation": .05, "technical": .17, "catalyst": .12, "institutional": .06, "liquidity": .04,
           "reward_risk": .08}


def _clip(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def _n(x, default=None):
    try:
        return None if x is None else float(x)
    except Exception:
        return default


def factor_scores(tech: dict, fund: dict, regime: dict, earnings: dict, policy: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    f = {}
    sector = sector_of(tech["symbol"])

    # 1 regime alignment
    base = {"aggressive_growth": 85, "growth": 75, "balanced": 65, "defensive": 50, "risk_off": 25}[regime["regime"]]
    if sector in regime.get("leaders", []):
        base += 10; notes.append(f"{sector} is a leading sector")
    if sector in regime.get("laggards", []):
        base -= 15; notes.append(f"{sector} is a lagging sector")
    f["regime"] = _clip(base)

    # 2 business quality: margins + ROE (facts from vendor; None -> neutral 55 with lower confidence)
    pm, roe = _n(fund.get("profitMargins")), _n(fund.get("returnOnEquity"))
    bq = 55
    if pm is not None:
        bq += 20 if pm > 0.20 else 10 if pm > 0.10 else -10 if pm < 0 else 0
    if roe is not None:
        bq += 15 if roe > 0.20 else 5 if roe > 0.10 else -10 if roe < 0 else 0
    f["business_quality"] = _clip(bq)

    # 3 financial strength: leverage, liquidity, FCF
    de, cr, fcf = _n(fund.get("debtToEquity")), _n(fund.get("currentRatio")), _n(fund.get("freeCashflow"))
    fs = 55
    if de is not None:
        fs += 15 if de < 50 else 5 if de < 100 else -10 if de > 200 else 0
    if cr is not None:
        fs += 5 if cr > 1.2 else -5
    if fcf is not None:
        fs += 10 if fcf > 0 else -15
    f["financial_strength"] = _clip(fs)

    # 4 growth
    rg, eg = _n(fund.get("revenueGrowth")), _n(fund.get("earningsGrowth"))
    g = 50
    if rg is not None:
        g += 25 if rg > 0.15 else 15 if rg > 0.08 else 5 if rg > 0.03 else -10 if rg < 0 else 0
    if eg is not None:
        g += 15 if eg > 0.15 else 5 if eg > 0.05 else -10 if eg < 0 else 0
    f["growth"] = _clip(g)

    # 5 moat proxy: size + margins stability (large caps default higher)
    mc = _n(fund.get("marketCap")) or 0
    moat = 50 + (20 if mc > 1e11 else 12 if mc > 3e10 else 5 if mc > 1e10 else 0) + (10 if (pm or 0) > 0.15 else 0)
    f["moat"] = _clip(moat)

    # 6 valuation: forward PE vs growth (PEG), analyst target vs price
    fpe, peg = _n(fund.get("forwardPE")), _n(fund.get("pegRatio"))
    val = 55
    if peg is not None:
        val += 15 if peg < 1.5 else 5 if peg < 2.5 else -10
    elif fpe is not None:
        val += 10 if fpe < 20 else 0 if fpe < 35 else -10
    tgt = _n(fund.get("targetMedianPrice"))
    if tgt:
        up = 100 * (tgt / tech["price"] - 1)
        val += 10 if up > 10 else 5 if up > 3 else -10 if up < 0 else 0
        if up < 0:
            notes.append("analyst median target is below price")
    f["valuation"] = _clip(val)

    # 7 technical
    ts, why = technical_score(tech)
    f["technical"] = ts
    notes.extend(why)

    # 8 catalyst: recent beat + continuation, or upcoming (reviewed) catalyst
    cat = 45
    pe = tech.get("post_earnings")
    sp = _n((earnings.get("surprise") or {}).get("surprise_pct"))
    if pe:
        cat += 20 if pe["reaction_chg_pct"] > 3 else 5 if pe["reaction_chg_pct"] > 0 else -15
        if sp is not None:
            cat += 10 if sp > 3 else 5 if sp > 0 else -10
        notes.append(f"post-earnings reaction {pe['reaction_chg_pct']:+.1f}% on {pe['reaction_vol_ratio']}x volume")
    elif sp is not None and sp > 0:
        cat += 10
    rec = _n(fund.get("recommendationMean"))
    if rec is not None:
        cat += 10 if rec <= 2.0 else 5 if rec <= 2.5 else -5 if rec > 3 else 0
    f["catalyst"] = _clip(cat)

    # 9 institutional
    inst = _n(fund.get("heldPercentInstitutions"))
    f["institutional"] = _clip(55 + (20 if (inst or 0) > 0.75 else 10 if (inst or 0) > 0.5 else 0) + (10 if tech["up_days_high_vol_20"] > tech["down_days_high_vol_20"] else 0))

    # 10 liquidity
    dv = tech["avg_dollar_volume"]
    f["liquidity"] = _clip(40 + (55 if dv > 5e8 else 45 if dv > 1e8 else 30 if dv > 5e7 else 10 if dv > 2e7 else -20))

    # 11 reward/risk
    rr = tech.get("expected_rr", 0)
    f["reward_risk"] = _clip(20 + 25 * rr) if rr else 10

    return f, notes


def penalties(tech: dict, earnings: dict, regime: dict, policy: dict, open_positions: list[dict], today: dt.date) -> list[tuple[str, float]]:
    pen = []
    if tech["price"] < tech["ema48"]:
        pen.append(("price below 48 EMA", 10))
    if tech["rs_3m_pct"] < 0:
        pen.append(("weak relative strength", 5))
    if earnings.get("inside_window"):
        pen.append(("earnings inside holding window", 10))
    if tech["ext_vs_21ema_pct"] > policy["max_extension_above_21ema_pct"]:
        pen.append(("extended above 21 EMA", 5))
    if tech["setup"] in ("new_52w_high", "breakout_watch") and (tech["vol_ratio_last"] or 1) < 0.8:
        pen.append(("new high on light volume", 3))
    cl, sec = cluster_of(tech["symbol"]), sector_of(tech["symbol"])
    for p in open_positions:
        if cluster_of(p["symbol"]) == cl and cl or sector_of(p["symbol"]) == sec:
            pen.append((f"correlated with open position {p['symbol']}", 5))
            break
    window_end = today + dt.timedelta(days=int(policy["setup_window_trading_days"] * 1.45))
    for d, label in (policy.get("macro_events") or {}).items():
        try:
            ev = dt.date.fromisoformat(d)
        except Exception:
            continue
        if today <= ev <= window_end:
            pen.append((f"binary macro event inside window: {label} ({d})", 3))
    if tech["atr_pct"] > policy["high_vol_atr_pct_step_down"]:
        pen.append(("high volatility (ATR > 5% of price)", 3))
    return pen


def wso_score(f: dict, pen: list[tuple[str, float]]) -> float:
    raw = sum(WEIGHTS[k] * f[k] for k in WEIGHTS)
    return round(max(0.0, min(100.0, raw - sum(p for _, p in pen))), 1)


def grade(score: float, policy: dict) -> str:
    if score >= policy["score_exceptional_min"]:
        return "exceptional"
    if score >= policy["score_strong_min"]:
        return "strong"
    if score >= policy["score_conditional_min"]:
        return "conditional"
    if score >= policy["score_watchlist_min"]:
        return "watchlist"
    return "reject"


def critic(tech: dict, f: dict, pen: list, earnings: dict, regime: dict) -> list[str]:
    """Rule-based bear case. The Critic must always find something."""
    bear = [f"Penalty: {p}" for p, _ in pen]
    if tech.get("expected_rr", 0) < 2:
        bear.append(f"Reward:risk {tech.get('expected_rr', 0):.1f} is below the 2.0 minimum at a {tech['stop_pct']:.1f}% stop")
    pe = tech.get("post_earnings")
    if pe and pe["reaction_chg_pct"] < 0:
        bear.append("Earnings reaction day closed red — continuation is unconfirmed")
    if f["valuation"] < 50:
        bear.append("Valuation offers little cushion if growth disappoints")
    if f["financial_strength"] < 50:
        bear.append("Balance-sheet leverage is above comfort")
    if regime.get("regime") in ("defensive", "risk_off"):
        bear.append("Regime does not favor new longs")
    if tech["setup"] == "extended":
        bear.append("Chasing an extended move invites a sharp mean reversion to the 21 EMA")
    if not bear:
        bear.append("Crowded, well-known name: upside may already be priced in; gap risk through the stop remains")
    return bear


def decide(score: float, tech: dict, earnings: dict, regime: dict, policy: dict) -> tuple[str, list[str]]:
    """Map score + hard rules to a CIO decision. Returns (decision, reasons)."""
    reasons = []
    g = grade(score, policy)
    if not regime.get("new_positions_allowed", True):
        return "REJECT", ["regime risk_off: no new positions"]
    if earnings.get("inside_window"):
        reasons.append("earnings inside holding window")
    if tech["setup"] in ("none", "extended", "pullback_below_21ema"):
        reasons.append(f"no actionable setup ({tech['setup']})")
    if tech["stop_pct"] > policy["stop_pct_of_position_max"]:
        reasons.append(f"invalidation {tech['stop_pct']:.1f}% exceeds 30% stop budget")
    if tech.get("expected_rr", 0) < policy["min_reward_risk"]:
        reasons.append(f"reward:risk {tech.get('expected_rr', 0):.2f} < {policy['min_reward_risk']}")
    if tech.get("data_quality") == "degraded":
        reasons.append("fundamental data unavailable from vendor — confidence reduced, re-run later")
    if g == "reject":
        return "REJECT", reasons + [f"score {score} below {policy['score_reject_below']}"]
    if g == "watchlist":
        return "WATCHLIST", reasons + [f"score {score} in watchlist band"]
    if reasons:  # score is high enough but a hard rule fails
        return "WATCHLIST", reasons
    if g == "conditional":
        return "APPROVE_WITH_REDUCED_SIZE", [f"score {score}: conditional candidate — half size"]
    return "APPROVE", [f"score {score}: {g} candidate"]
