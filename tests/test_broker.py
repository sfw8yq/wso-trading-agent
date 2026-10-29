import datetime as dt
import wso.paper_broker as PB
import wso.pipeline as PL
from wso import policy as P
from wso.store import default_state
from wso.reports import build_report, performance

NOW = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def _quote(price, lo=None, hi=None):
    return lambda sym: {"symbol": sym, "price": price, "day_low": lo or price, "day_high": hi or price, "prev_close": price, "timestamp_utc": NOW(), "source": "mock"}

def _state():
    s = default_state(); s["account"]["cash"] = 10000; s["account"]["starting_capital"] = 10000; return s

def _packet():
    return {"account": "PAPER", "symbol": "IQV", "instrument": "equity", "side": "buy", "strategy": "pullback_to_21ema", "quantity": 5,
            "limit_price": 280.0, "reference_price": 279.5, "stop_price": 266.0, "target_1": 292.0, "target_2": 305.0, "expected_rr": 2.4,
            "score": 80, "decision": "APPROVE", "market_regime": "balanced", "order_type": "limit", "earnings_inside_window": False,
            "quote_timestamp_utc": NOW(), "user_approved": True, "max_hold_trading_days": 30, "setup_window_trading_days": 15}

def test_fill_stop_and_report(monkeypatch):
    monkeypatch.setattr(PB, "latest_quote", _quote(279.0))
    monkeypatch.setattr(PB, "intraday_bars", lambda sym, d=2: None)
    monkeypatch.setattr(PB, "next_earnings_date", lambda sym: (None, None, {}))
    s = _state(); pol = P.merged(); b = PB.PaperBroker(s, pol)
    o = b.place_limit_buy(_packet(), "fp1")
    assert o["status"] == "filled" and len(s["positions"]) == 1
    assert s["account"]["cash"] < 10000
    # stop hit
    monkeypatch.setattr(PB, "latest_quote", _quote(264.0, lo=263.0))
    ev = b.monitor()
    assert ev and ev[0]["type"] == "stop_hit" and not s["positions"]
    t = s["trades"][-1]
    assert t["pnl"] < 0 and abs(t["pnl"]) <= 5 * (280 - 266) * 1.2   # gap-through fill, still bounded by size
    perf = performance(s, dt.date.today())
    assert perf["total_trades"] == 1 and perf["win_rate"] == 0
    rep = build_report(s, "daily"); assert "Daily performance report" in rep["html"]

def test_partial_at_t1_then_t2(monkeypatch):
    monkeypatch.setattr(PB, "latest_quote", _quote(279.0))
    monkeypatch.setattr(PB, "intraday_bars", lambda sym, d=2: None)
    monkeypatch.setattr(PB, "next_earnings_date", lambda sym: (None, None, {}))
    s = _state(); pol = P.merged(); b = PB.PaperBroker(s, pol)
    b.place_limit_buy(_packet(), "fp2")
    monkeypatch.setattr(PB, "latest_quote", _quote(293.0, hi=293.5))
    ev = b.monitor()
    assert ev[0]["type"] == "target_1_partial" and s["positions"][0]["t1_hit"] and s["positions"][0]["stop"] >= s["positions"][0]["entry_price"]
    monkeypatch.setattr(PB, "latest_quote", _quote(306.0, hi=306.0))
    ev = b.monitor()
    assert ev[0]["type"] == "target_2_hit" and not s["positions"]
    assert sum(t["pnl"] for t in s["trades"]) > 0

def test_exit_before_earnings(monkeypatch):
    monkeypatch.setattr(PB, "latest_quote", _quote(279.0))
    monkeypatch.setattr(PB, "intraday_bars", lambda sym, d=2: None)
    monkeypatch.setattr(PB, "next_earnings_date", lambda sym: (dt.date.today() + dt.timedelta(days=1), None, {}))
    s = _state(); b = PB.PaperBroker(s, P.merged())
    b.place_limit_buy(_packet(), "fp3")
    ev = b.monitor()
    assert ev[0]["type"] == "exit_before_earnings"

def test_open_order_not_filled_above_limit(monkeypatch):
    monkeypatch.setattr(PB, "latest_quote", _quote(285.0))
    s = _state(); b = PB.PaperBroker(s, P.merged())
    o = b.place_limit_buy(_packet(), "fp4")
    assert o["status"] == "open" and not s["positions"]

def test_reset_capital():
    s = _state(); b = PB.PaperBroker(s, P.merged()); b.reset_capital(2500)
    assert s["account"]["cash"] == 2500 and s["settings"]["trading_capital"] == 2500
