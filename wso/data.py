"""Market data layer (yfinance). Every value returned carries a timestamp so
the risk engine can enforce freshness. Nothing here is ever invented: when a
field is unavailable it is returned as None and flagged upstream.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import warnings

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

NY = "America/New_York"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_ny() -> dt.datetime:
    return now_utc().astimezone(_tz(NY))


def _tz(name):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def is_market_open(ts: dt.datetime | None = None) -> bool:
    t = (ts or now_ny()).astimezone(_tz(NY))
    if t.weekday() >= 5:
        return False
    o = t.replace(hour=9, minute=30, second=0, microsecond=0)
    c = t.replace(hour=16, minute=0, second=0, microsecond=0)
    return o <= t <= c


def trading_days_between(a: dt.date, b: dt.date) -> int:
    """Approximate count of NYSE trading days in (a, b] (weekdays only)."""
    if b <= a:
        return 0
    return int(pd.bdate_range(a + dt.timedelta(days=1), b).size)


def download_history(symbols: list[str], period: str = "1y", interval: str = "1d",
                     chunk: int = 60, retries: int = 2) -> dict[str, pd.DataFrame]:
    """Return {symbol: DataFrame[open, high, low, close, volume]} (adjusted)."""
    out: dict[str, pd.DataFrame] = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        for attempt in range(retries + 1):
            try:
                raw = yf.download(batch, period=period, interval=interval, auto_adjust=True,
                                  progress=False, group_by="ticker", threads=True)
                break
            except Exception:
                if attempt == retries:
                    raw = pd.DataFrame()
                time.sleep(1.5)
        for s in batch:
            try:
                df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
                if len(df) >= 30:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    out[s] = df
            except Exception:
                continue
    return out


def latest_quote(symbol: str) -> dict:
    """Best-effort last trade. Returns price, timestamp (UTC), source, day high/low."""
    t = yf.Ticker(symbol)
    price = None
    try:
        fi = t.fast_info
        price = fi.get("lastPrice") or fi.get("last_price")
    except Exception:
        pass
    day_high = day_low = prev_close = None
    try:
        h = t.history(period="2d", interval="1d", auto_adjust=True)
        if len(h):
            day_high, day_low = float(h["High"].iloc[-1]), float(h["Low"].iloc[-1])
            if len(h) > 1:
                prev_close = float(h["Close"].iloc[-2])
            if price is None:
                price = float(h["Close"].iloc[-1])
    except Exception:
        pass
    return {"symbol": symbol, "price": float(price) if price else None, "day_high": day_high, "day_low": day_low,
            "prev_close": prev_close, "timestamp_utc": now_utc().isoformat(timespec="seconds"), "source": "yfinance"}


def intraday_bars(symbol: str, lookback_days: int = 5) -> pd.DataFrame | None:
    """15-minute bars; used by the paper monitor to detect stop/target touches."""
    try:
        df = yf.Ticker(symbol).history(period=f"{lookback_days}d", interval="15m", auto_adjust=True)
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        return df
    except Exception:
        return None


def fundamentals(symbol: str) -> dict:
    """Subset of yfinance .info with None for missing fields (never invented)."""
    keys = ["marketCap", "sector", "industry", "averageVolume", "trailingPE", "forwardPE", "pegRatio",
            "revenueGrowth", "earningsGrowth", "profitMargins", "operatingMargins", "returnOnEquity",
            "debtToEquity", "currentRatio", "freeCashflow", "totalCash", "totalDebt", "heldPercentInstitutions",
            "recommendationMean", "numberOfAnalystOpinions", "targetMedianPrice", "targetMeanPrice",
            "targetHighPrice", "targetLowPrice", "shortPercentOfFloat", "beta", "longName"]
    info = {k: None for k in keys}
    raw = {}
    for attempt in range(3):                      # vendor rate-limits this endpoint; back off and retry
        try:
            raw = yf.Ticker(symbol).info or {}
            if raw.get("marketCap") or raw.get("regularMarketPrice"):
                break
        except Exception:
            raw = {}
        time.sleep(1.5 * (attempt + 1))
    for k in keys:
        info[k] = raw.get(k)
    if info["marketCap"] is None:                 # secondary source for the one field the screen needs
        try:
            info["marketCap"] = yf.Ticker(symbol).fast_info.get("marketCap")
        except Exception:
            pass
    info["data_quality"] = "ok" if raw.get("marketCap") or raw.get("profitMargins") is not None else "degraded"
    info["fetched_at_utc"] = now_utc().isoformat(timespec="seconds")
    return info


def next_earnings_date(symbol: str) -> tuple[dt.date | None, dt.date | None, dict]:
    """(next_date, last_reported_date, last_surprise) from yfinance earnings calendar."""
    nxt, last, surprise = None, None, {}
    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if ed is not None and len(ed):
            today = dt.date.today()
            idx = pd.to_datetime(ed.index)
            dates = [d.date() for d in idx]
            fut = sorted([d for d in dates if d >= today])
            past = sorted([d for d in dates if d < today])
            nxt = fut[0] if fut else None
            if past:
                last = past[-1]
                row = ed.iloc[[i for i, d in enumerate(dates) if d == last][0]]
                surprise = {"eps_estimate": _f(row.get("EPS Estimate")), "eps_actual": _f(row.get("Reported EPS")),
                            "surprise_pct": _f(row.get("Surprise(%)"))}
    except Exception:
        pass
    return nxt, last, surprise


def _f(x):
    try:
        v = float(x)
        return None if pd.isna(v) else v
    except Exception:
        return None
