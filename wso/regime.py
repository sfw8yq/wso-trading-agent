"""Gate 2 — market regime engine.

Deterministic score (0–100) from index trend, volatility, breadth and sector
leadership. The regime sets the position-size tier and whether new
positions are allowed at all.
"""
from __future__ import annotations

import pandas as pd

from .data import download_history, now_utc
from .universe import REGIME_SYMBOLS, SECTOR_ETF


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def classify(score: float) -> str:
    if score >= 75:
        return "aggressive_growth"
    if score >= 62:
        return "growth"
    if score >= 48:
        return "balanced"
    if score >= 35:
        return "defensive"
    return "risk_off"


def compute_regime(hist: dict[str, pd.DataFrame] | None = None, universe_hist: dict[str, pd.DataFrame] | None = None) -> dict:
    hist = hist or download_history(REGIME_SYMBOLS, period="1y")
    facts, score, notes = {}, 0.0, []

    def trend_block(sym: str) -> dict:
        df = hist.get(sym)
        if df is None:
            return {}
        c = df["close"]
        e21, e48, e200 = ema(c, 21), ema(c, 48), ema(c, 200)
        hi52 = df["high"].iloc[-252:].max()
        return {"close": round(float(c.iloc[-1]), 2), "ema21": round(float(e21.iloc[-1]), 2),
                "ema48": round(float(e48.iloc[-1]), 2), "ema200": round(float(e200.iloc[-1]), 2),
                "above21": bool(c.iloc[-1] > e21.iloc[-1]), "above48": bool(c.iloc[-1] > e48.iloc[-1]),
                "above200": bool(c.iloc[-1] > e200.iloc[-1]), "ema21_gt_48": bool(e21.iloc[-1] > e48.iloc[-1]),
                "pct_from_52w_high": round(100 * (float(c.iloc[-1]) / float(hi52) - 1), 2),
                "ret_1m_pct": round(100 * (float(c.iloc[-1]) / float(c.iloc[-22]) - 1), 2) if len(c) > 22 else None,
                "as_of": str(df.index[-1].date())}

    for sym in ["SPY", "QQQ", "IWM", "SOXX", "DIA"]:
        facts[sym] = trend_block(sym)

    spy = facts.get("SPY", {})
    if spy:
        score += 12 if spy["above21"] else 0
        score += 12 if spy["above48"] else 0
        score += 8 if spy["above200"] else 0
        score += 5 if spy["ema21_gt_48"] else 0
        score += 3 if spy["pct_from_52w_high"] >= -2 else 0
        if not spy["above48"]:
            notes.append("SPY below 48 EMA: intermediate trend broken")
    q = facts.get("QQQ", {})
    if q:
        score += 6 if q["above21"] else -6
        if (q.get("ret_1m_pct") or 0) < 0:
            score -= 3; notes.append("QQQ negative over 1 month: growth leadership fading")
        if not q["above21"]:
            notes.append("QQQ below 21 EMA")
    r = facts.get("IWM", {})
    if r:
        score += 3 if r["above48"] else -3
        if not r["above48"]:
            notes.append("IWM below 48 EMA: small caps lagging")
    sx = facts.get("SOXX", {})
    if sx:
        score += 5 if sx["above48"] else -5
        if not sx["above48"]:
            notes.append("SOXX below 48 EMA: semis/AI-infra penalized")

    # volatility
    vix = None
    if "^VIX" in hist:
        vix = float(hist["^VIX"]["close"].iloc[-1])
        facts["VIX"] = round(vix, 2)
        if vix < 15:
            score += 12
        elif vix < 20:
            score += 8
        elif vix < 25:
            score += 3
        elif vix >= 30:
            score -= 10
            notes.append("VIX >= 30: risk-off volatility")

    # breadth proxy: % of universe above 48 EMA
    breadth = None
    if universe_hist:
        above = [float(d["close"].iloc[-1] > ema(d["close"], 48).iloc[-1]) for d in universe_hist.values() if len(d) > 60]
        if above:
            breadth = 100 * sum(above) / len(above)
            score += 25 * breadth / 100
            facts["breadth_pct_above_48ema"] = round(breadth, 1)
    else:
        score += 12  # neutral assumption when breadth not computed

    # sector leadership (1-month return)
    sectors = {}
    for name, etf in SECTOR_ETF.items():
        df = hist.get(etf)
        if df is not None and len(df) > 22:
            sectors[name] = round(100 * (float(df["close"].iloc[-1]) / float(df["close"].iloc[-22]) - 1), 2)
    ranked = sorted(sectors.items(), key=lambda kv: kv[1], reverse=True)
    leaders = [k for k, _ in ranked[:4]]
    laggards = [k for k, _ in ranked[-3:]]
    if sx and not sx["above48"] and "Semiconductors" not in laggards:
        laggards.append("Semiconductors")
    if sum(1 for k in leaders if k in ("Utilities", "Consumer Staples", "Health Care")) >= 2:
        score -= 5
        notes.append("defensive sectors leading: rotation away from risk")

    score = max(0.0, min(100.0, score))
    regime = classify(score)
    return {"regime": regime, "score": round(score, 1), "computed_at_utc": now_utc().isoformat(timespec="seconds"),
            "as_of_close": spy.get("as_of"), "facts": facts, "sector_1m_returns": dict(ranked),
            "leaders": leaders, "laggards": laggards, "notes": notes,
            "new_positions_allowed": regime != "risk_off",
            "label": "fact: computed from daily closes; regime label is a rule-based inference"}
