import datetime as dt
from wso import policy as P
from wso.risk_engine import evaluate, account_view, size_proposal
from wso.store import default_state

def _state(cash=10000, approval=True):
    s = default_state(); s["account"]["cash"] = cash; s["account"]["starting_capital"] = cash
    s["settings"]["human_approval"] = approval; return s

def _packet(**kw):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    p = {"account": "PAPER", "symbol": "IQV", "instrument": "equity", "side": "buy", "strategy": "pullback_to_21ema",
         "quantity": 5, "limit_price": 280.0, "reference_price": 279.5, "stop_price": 266.0, "expected_rr": 2.5, "score": 80,
         "decision": "APPROVE", "market_regime": "balanced", "order_type": "limit", "earnings_inside_window": False,
         "quote_timestamp_utc": now, "user_approved": True}
    p.update(kw); return p

def test_balanced_size_passes_precheck():
    s = _state(); pol = P.merged()
    r = evaluate(_packet(), s, pol, account_view(s), pre_check=True)
    fails = [m for st, m in r["checks"] if st == "FAIL"]
    assert not fails, fails

def test_oversize_rejected():
    s = _state(); pol = P.merged()
    r = evaluate(_packet(quantity=6), s, pol, account_view(s), pre_check=True)   # 6*280=1680 > 15%
    assert not r["approved"] and any("size" in m for st, m in r["checks"] if st == "FAIL")

def test_stop_wider_than_30pct_rejected():
    s = _state(); pol = P.merged()
    r = evaluate(_packet(stop_price=190.0), s, pol, account_view(s), pre_check=True)
    assert any("stop distance" in m for st, m in r["checks"] if st == "FAIL")

def test_low_score_or_rr_rejected():
    s = _state(); pol = P.merged()
    r = evaluate(_packet(score=70), s, pol, account_view(s), pre_check=True); assert not r["approved"]
    r = evaluate(_packet(expected_rr=1.5), s, pol, account_view(s), pre_check=True); assert not r["approved"]

def test_approval_required_when_on():
    s = _state(approval=True); pol = P.merged()
    r = evaluate(_packet(user_approved=False), s, pol, account_view(s), pre_check=True)
    assert any("human approval" in m for st, m in r["checks"] if st == "FAIL")

def test_no_averaging_into_existing_position():
    s = _state(); pol = P.merged()
    s["positions"].append({"symbol": "IQV", "quantity": 1, "entry_price": 270, "stop": 260, "last_price": 280})
    r = evaluate(_packet(), s, pol, account_view(s), pre_check=True)
    assert any("never add" in m for st, m in r["checks"] if st == "FAIL")

def test_size_proposal_respects_allocation():
    s = _state(); pol = P.merged()
    tech = {"symbol": "IQV", "atr_pct": 3.0, "stop": 266.0, "t1": 290.0, "t2": 300.0, "expected_rr": 2.5, "setup": "pullback_to_21ema"}
    pk = size_proposal(tech, {"decision": "APPROVE", "score": 80}, "balanced", account_view(s), pol,
                       {"price": 279.5, "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat()})
    assert pk["quantity"] * pk["limit_price"] <= 1500.0 + 1e-6
    assert pk["limit_price"] <= 279.5 * 1.005

def test_bounds_enforced():
    pol = P.merged({"size_pct_balanced": 60, "min_reward_risk": 1.0})
    assert pol["size_pct_balanced"] == 25 and pol["min_reward_risk"] == 2.0
