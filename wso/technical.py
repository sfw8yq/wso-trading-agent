"""Technical specialist: indicators, setup classification, invalidation
(stop) and target levels. All numbers are computed from daily bars —
nothing is estimated by hand.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .regime import ema


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def analyze(symbol: str, df: pd.DataFrame, spy: pd.DataFrame, last_earnings: dt.date | None = None) -> dict:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    e21, e48, e200 = ema(c, 21), ema(c, 48), ema(c, 200)
    a = atr(df)
    px = float(c.iloc[-1])
    a14 = float(a.iloc[-1]) if not np.isnan(a.iloc[-1]) else px * 0.02
    hi52 = float(h.iloc[-252:].max())
    lo10 = float(l.iloc[-10:].min())
    lo20 = float(l.iloc[-20:].min())
    avgv50 = float(v.iloc[-51:-1].mean()) if len(v) > 51 else float(v.mean())
    dollar_vol = avgv50 * px

    # relative strength vs SPY (aligned on dates)
    def rs(n):
        try:
            s2 = spy["close"].reindex(df.index).ffill()
            return 100 * ((c.iloc[-1] / c.iloc[-n - 1]) / (s2.iloc[-1] / s2.iloc[-n - 1]) - 1)
        except Exception:
            return 0.0

    rs1m, rs3m = float(rs(21)), float(rs(63))
    chg = c.diff()
    up_hv = int(((chg > 0) & (v > avgv50)).iloc[-20:].sum())
    dn_hv = int(((chg < 0) & (v > avgv50)).iloc[-20:].sum())
    ext21 = 100 * (px / float(e21.iloc[-1]) - 1)
    from_hi = 100 * (px / hi52 - 1)
    e21_slope = float(e21.iloc[-1] - e21.iloc[-6])
    above_all = px > e21.iloc[-1] > e48.iloc[-1] and px > e200.iloc[-1]

    # earnings reaction (post-earnings continuation)
    pec = None
    if last_earnings is not None:
        # reaction day = first session on/after report date
        after = df[df.index.date >= last_earnings]
        if len(after) and len(after) <= 12:
            rd = after.iloc[0]
            prev_i = df.index.get_loc(after.index[0]) - 1
            if prev_i >= 0:
                prev_close = float(df["close"].iloc[prev_i])
                gap_chg = 100 * (float(rd["close"]) / prev_close - 1)
                pec = {"reaction_date": str(after.index[0].date()), "reaction_chg_pct": round(gap_chg, 2),
                       "reaction_vol_ratio": round(float(rd["volume"]) / avgv50, 2) if avgv50 else None,
                       "reaction_low": float(rd["low"]), "holding_above_low": px > float(rd["low"])}

    # ---- setup classification -------------------------------------------
    setup = "none"
    if above_all and e21_slope > 0:
        if pec and pec["reaction_chg_pct"] >= 3 and (pec["reaction_vol_ratio"] or 0) >= 1.5 and pec["holding_above_low"] and 0 <= ext21 <= 8:
            setup = "post_earnings_continuation"
        elif -1.0 <= ext21 <= 3.0 and rs3m > 0:
            setup = "pullback_to_21ema"
        elif from_hi >= -3.0 and ext21 <= 7.0:
            setup = "breakout_watch" if px < hi52 else "new_52w_high"
        elif ext21 > 7.0:
            setup = "extended"
    elif px > e48.iloc[-1] and px < e21.iloc[-1] and e48.iloc[-1] > e200.iloc[-1]:
        setup = "pullback_below_21ema"

    # ---- invalidation & targets ------------------------------------------
    # Stop = technical invalidation: below the 21 EMA and the recent swing low,
    # with a quarter-ATR buffer. The policy caps distance at 30% of position.
    if setup == "post_earnings_continuation" and pec:
        inval = min(float(e21.iloc[-1]), pec["reaction_low"])
    elif setup in ("pullback_to_21ema", "pullback_below_21ema"):
        inval = min(float(e48.iloc[-1]), lo10)
    else:
        inval = min(float(e21.iloc[-1]), lo10)
    stop = round(inval - 0.25 * a14, 2)
    stop_pct = 100 * (px - stop) / px

    # T1 = nearest structural level (52w high if >= 2% away, else +2 ATR). T2 = analyst median target if
    # above T1, else +4 ATR. Expected R:R is the blended average, exactly as in the manual process.
    t1 = hi52 if from_hi <= -2 else px + 2 * a14
    t2 = px + 4 * a14
    return {
        "symbol": symbol, "as_of": str(df.index[-1].date()), "price": round(px, 2),
        "ema21": round(float(e21.iloc[-1]), 2), "ema48": round(float(e48.iloc[-1]), 2), "ema200": round(float(e200.iloc[-1]), 2),
        "ema21_slope_5d": round(e21_slope, 2), "atr14": round(a14, 2), "atr_pct": round(100 * a14 / px, 2),
        "ext_vs_21ema_pct": round(ext21, 2), "pct_from_52w_high": round(from_hi, 2), "hi52": round(hi52, 2),
        "low10": round(lo10, 2), "low20": round(lo20, 2), "rs_1m_pct": round(rs1m, 2), "rs_3m_pct": round(rs3m, 2),
        "vol_ratio_last": round(float(v.iloc[-1]) / avgv50, 2) if avgv50 else None,
        "up_days_high_vol_20": up_hv, "down_days_high_vol_20": dn_hv,
        "avg_dollar_volume": round(dollar_vol, 0), "above_all_emas": bool(above_all),
        "setup": setup, "post_earnings": pec, "stop": stop, "stop_pct": round(stop_pct, 2),
        "t1": round(t1, 2), "t2": round(t2, 2),
        "ret_1m_pct": round(100 * (px / float(c.iloc[-22]) - 1), 2) if len(c) > 22 else None,
        "ret_3m_pct": round(100 * (px / float(c.iloc[-64]) - 1), 2) if len(c) > 64 else None,
    }


def apply_analyst_target(tech: dict, median_target: float | None) -> dict:
    """Refine T2 with the analyst median target when it is above T1 (fact from data vendor)."""
    if median_target and median_target > tech["t1"]:
        tech["t2"] = round(float(median_target), 2)
        tech["t2_source"] = "analyst_median_target"
    else:
        tech["t2_source"] = "4xATR"
    px, stop = tech["price"], tech["stop"]
    r = px - stop
    if r <= 0:
        tech["rr_t1"] = tech["rr_t2"] = tech["expected_rr"] = 0.0
        return tech
    tech["rr_t1"] = round((tech["t1"] - px) / r, 2)
    tech["rr_t2"] = round((tech["t2"] - px) / r, 2)
    tech["expected_rr"] = round((tech["rr_t1"] + tech["rr_t2"]) / 2, 2)
    return tech


def technical_score(t: dict) -> tuple[float, list[str]]:
    """0–100 technical sub-score with reasons."""
    s, why = 50.0, []
    if t["above_all_emas"]:
        s += 15; why.append("above 21/48/200 EMA")
    else:
        s -= 15; why.append("not above all EMAs")
    if t["ema21_slope_5d"] > 0:
        s += 5
    if t["rs_3m_pct"] > 5:
        s += 10; why.append(f"RS 3m +{t['rs_3m_pct']:.1f}% vs SPY")
    elif t["rs_3m_pct"] < 0:
        s -= 10; why.append("weak 3-month relative strength")
    if t["rs_1m_pct"] > 0:
        s += 5
    if 0 <= t["ext_vs_21ema_pct"] <= 7:
        s += 10
    elif t["ext_vs_21ema_pct"] > 7:
        s -= 10; why.append(f"extended {t['ext_vs_21ema_pct']:.1f}% above 21 EMA")
    if t["pct_from_52w_high"] >= -3:
        s += 5; why.append("within 3% of 52-week high")
    if t["up_days_high_vol_20"] > t["down_days_high_vol_20"]:
        s += 5; why.append("accumulation: more high-volume up days")
    elif t["down_days_high_vol_20"] > t["up_days_high_vol_20"] + 1:
        s -= 5; why.append("distribution: more high-volume down days")
    if t["setup"] in ("post_earnings_continuation", "pullback_to_21ema", "new_52w_high", "breakout_watch"):
        s += 5
    return max(0.0, min(100.0, s)), why
