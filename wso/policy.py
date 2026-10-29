"""Risk policy (v2) — the numeric rules the whole system obeys.

Values come from the user's written instructions (2026-10-29) plus the
conservative defaults the user delegated. Everything here is editable from
the Settings page inside the bounds the user originally set.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

_HERE = Path(__file__).parent
_DEFAULT_FILE = _HERE / "policy_defaults.json"

# Flat, UI-friendly view of the policy. Keys map onto the nested JSON above.
FLAT_DEFAULTS: dict = {
    "policy_version": "2026-10-29.v2",
    # --- sizing (user-defined) ---
    "size_pct_aggressive_growth": 25.0,
    "size_pct_growth": 20.0,
    "size_pct_balanced": 15.0,
    "size_pct_defensive": 10.0,
    "size_pct_risk_off": 0.0,
    "size_pct_hard_max": 25.0,
    "high_vol_atr_pct_step_down": 5.0,      # ATR/price above this -> one tier smaller
    # --- per-trade loss (user-defined, per POSITION) ---
    "stop_pct_of_position_min": 20.0,
    "stop_pct_of_position_max": 30.0,
    "hard_cap_loss_pct_of_account": 7.5,
    # --- portfolio limits ---
    "max_portfolio_heat_pct": 15.0,
    "max_gross_exposure_pct": 80.0,
    "min_cash_reserve_pct": 20.0,
    "max_positions": 4,
    "max_positions_per_sector": 2,
    "max_sector_exposure_pct": 40.0,
    # --- circuit breakers ---
    "daily_loss_limit_pct": 5.0,
    "weekly_loss_limit_pct": 10.0,
    "consecutive_losses_pause": 3,
    # --- execution ---
    "equity_limit_band_pct": 0.5,          # limit <= ask * (1 + band)
    "quote_max_age_seconds": 120,
    "account_max_age_seconds": 300,
    "trading_hours_only": True,
    # --- holding period ---
    "setup_window_trading_days": 15,
    "max_hold_trading_days": 30,
    # --- scoring thresholds ---
    "score_reject_below": 65,
    "score_watchlist_min": 65,
    "score_conditional_min": 75,
    "score_strong_min": 82,
    "score_exceptional_min": 90,
    "min_score_to_propose": 75,
    "min_reward_risk": 2.0,
    # --- screening ---
    "list_a_min_market_cap": 10_000_000_000,
    "list_a_min_avg_dollar_volume": 50_000_000,
    "list_b_min_market_cap": 2_000_000_000,
    "list_b_min_avg_dollar_volume": 20_000_000,
    "min_share_price": 5.0,
    "max_extension_above_21ema_pct": 7.0,
    "max_distance_from_52w_high_pct": 5.0,
    # --- options (kept for completeness; paper engine trades shares only) ---
    "options_enabled": False,
    "options_dte_min": 7,
    "options_dte_max": 30,
    "options_abs_delta_min": 0.45,
    "options_abs_delta_max": 0.70,
    "options_min_open_interest": 500,
    "options_min_volume": 100,
    "options_max_spread_pct_of_mid": 10.0,
    "options_exit_dte": 5,
    # --- paper trading realism ---
    "paper_slippage_pct": 0.10,
    "paper_fractional_shares": True,
    # --- known binary macro events (YYYY-MM-DD: label) ---
    "macro_events": {"2026-11-03": "US midterm elections"},
}

# Hard bounds: the Settings page cannot push a value outside the user's
# original instructions.
BOUNDS: dict = {
    "size_pct_aggressive_growth": (0, 25),
    "size_pct_growth": (0, 25),
    "size_pct_balanced": (0, 25),
    "size_pct_defensive": (0, 25),
    "size_pct_risk_off": (0, 0),
    "stop_pct_of_position_min": (5, 30),
    "stop_pct_of_position_max": (10, 30),
    "hard_cap_loss_pct_of_account": (0.5, 7.5),
    "max_portfolio_heat_pct": (1, 15),
    "max_gross_exposure_pct": (10, 80),
    "max_positions": (1, 4),
    "max_positions_per_sector": (1, 2),
    "daily_loss_limit_pct": (1, 5),
    "weekly_loss_limit_pct": (2, 10),
    "consecutive_losses_pause": (1, 3),
    "min_score_to_propose": (75, 100),
    "min_reward_risk": (2.0, 5.0),
    "setup_window_trading_days": (5, 15),
    "max_hold_trading_days": (5, 30),
}

REGIMES = ["aggressive_growth", "growth", "balanced", "defensive", "risk_off"]


def load_nested_defaults() -> dict:
    with open(_DEFAULT_FILE) as f:
        return json.load(f)


def merged(overrides: dict | None = None) -> dict:
    """Return the flat policy with user overrides applied and bounds enforced."""
    p = copy.deepcopy(FLAT_DEFAULTS)
    for k, v in (overrides or {}).items():
        if k not in p:
            continue
        if k in BOUNDS and isinstance(v, (int, float)):
            lo, hi = BOUNDS[k]
            v = max(lo, min(hi, v))
        p[k] = v
    return p


def size_pct_for_regime(policy: dict, regime: str, step_down: int = 0) -> float:
    order = REGIMES
    idx = order.index(regime) if regime in order else 2
    idx = min(len(order) - 1, idx + step_down)
    return float(policy[f"size_pct_{order[idx]}"])


def summary_lines(policy: dict) -> list[str]:
    return [
        f"Position size by regime: {policy['size_pct_aggressive_growth']:.0f}/{policy['size_pct_growth']:.0f}/"
        f"{policy['size_pct_balanced']:.0f}/{policy['size_pct_defensive']:.0f}/{policy['size_pct_risk_off']:.0f}% of buying power "
        "(aggressive / growth / balanced / defensive / risk-off).",
        f"Stop: {policy['stop_pct_of_position_min']:.0f}–{policy['stop_pct_of_position_max']:.0f}% of the POSITION at technical "
        f"invalidation (tighter is always allowed); hard cap {policy['hard_cap_loss_pct_of_account']}% of account per trade.",
        f"Portfolio: heat ≤ {policy['max_portfolio_heat_pct']:.0f}%, gross ≤ {policy['max_gross_exposure_pct']:.0f}%, "
        f"cash reserve ≥ {policy['min_cash_reserve_pct']:.0f}%, ≤ {policy['max_positions']} positions, "
        f"≤ {policy['max_positions_per_sector']} per sector.",
        f"Breakers: daily −{policy['daily_loss_limit_pct']:.0f}%, weekly −{policy['weekly_loss_limit_pct']:.0f}%, "
        f"pause after {policy['consecutive_losses_pause']} consecutive losses.",
        "Execution: limit orders only, day time-in-force, quote ≤ "
        f"{policy['quote_max_age_seconds']} s old, trading hours only, never average down or size up after a loss.",
        f"Horizon: setup must work within {policy['setup_window_trading_days']} trading days; max hold {policy['max_hold_trading_days']}.",
        f"Gate to propose an order: WSO score ≥ {policy['min_score_to_propose']} and reward:risk ≥ {policy['min_reward_risk']:.1f}. "
        "A high score never authorizes an order by itself.",
        "Options: long calls/puts/debit spreads only, 7–30 DTE, |delta| 0.45–0.70; rejected whenever one contract's premium exceeds the loss budget.",
    ]


def env_flag(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
