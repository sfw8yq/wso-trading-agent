"""Paper broker — simulates fills and manages open positions under the exit
rules (hard stop, partial at T1 + breakeven stop, full exit at T2, time
stop, earnings guard). Produces the same events a live broker adapter would,
so the notifier and reports don't care which one is in use.
"""
from __future__ import annotations

import datetime as dt
import uuid

from .data import intraday_bars, latest_quote, next_earnings_date, now_utc, trading_days_between


def _id() -> str:
    return uuid.uuid4().hex[:10]


class PaperBroker:
    def __init__(self, state: dict, policy: dict):
        self.s, self.p = state, policy

    # ---------------------------------------------------------------- orders
    def place_limit_buy(self, packet: dict, fingerprint: str) -> dict:
        """Fill immediately if the last price is at/below the limit (simulated
        marketable limit); otherwise leave the day order open for the monitor."""
        q = latest_quote(packet["symbol"])
        order = {"id": _id(), "symbol": packet["symbol"], "side": "buy", "quantity": packet["quantity"],
                 "limit_price": packet["limit_price"], "status": "open", "created_at_utc": now_utc().isoformat(timespec="seconds"),
                 "fingerprint": fingerprint, "packet": packet, "fills": []}
        self.s["fingerprints"].append(fingerprint)
        self.s["fingerprints"] = self.s["fingerprints"][-500:]
        px = q.get("price")
        if px is not None and px <= packet["limit_price"]:
            self._fill_buy(order, min(px * (1 + self.p["paper_slippage_pct"] / 100), packet["limit_price"]))
        self.s["orders"].append(order)
        return order

    def _fill_buy(self, order: dict, fill_px: float) -> None:
        pk = order["packet"]
        cost = fill_px * order["quantity"]
        if cost > self.s["account"]["cash"] + 1e-6:
            order["status"] = "rejected_insufficient_cash"
            return
        self.s["account"]["cash"] -= cost
        order.update(status="filled", filled_at_utc=now_utc().isoformat(timespec="seconds"), fill_price=round(fill_px, 4))
        order["fills"].append({"price": round(fill_px, 4), "quantity": order["quantity"], "at": order["filled_at_utc"]})
        self.s["positions"].append({
            "id": _id(), "symbol": pk["symbol"], "quantity": order["quantity"], "entry_price": round(fill_px, 4),
            "entry_date": str(dt.date.today()), "entry_at_utc": order["filled_at_utc"], "stop": pk["stop_price"],
            "initial_stop": pk["stop_price"], "target_1": pk["target_1"], "target_2": pk["target_2"], "t1_hit": False,
            "strategy": pk["strategy"], "score": pk["score"], "regime": pk["market_regime"], "last_price": round(fill_px, 4),
            "max_hold_trading_days": pk["max_hold_trading_days"], "setup_window_trading_days": pk["setup_window_trading_days"],
            "order_id": order["id"], "realized_partial": 0.0, "notes": []})

    def cancel_open_orders_eod(self) -> list[dict]:
        cancelled = []
        for o in self.s["orders"]:
            if o["status"] == "open" and o["created_at_utc"][:10] < str(dt.date.today()):
                o["status"] = "cancelled_eod"
                cancelled.append(o)
        return cancelled

    def try_fill_open_orders(self) -> list[dict]:
        filled = []
        for o in self.s["orders"]:
            if o["status"] != "open":
                continue
            q = latest_quote(o["symbol"])
            lo = q.get("day_low") or q.get("price")
            if lo is not None and lo <= o["limit_price"]:
                self._fill_buy(o, min(o["limit_price"], (q.get("price") or o["limit_price"])))
                if o["status"] == "filled":
                    filled.append(o)
        return filled

    # ------------------------------------------------------------- positions
    def close(self, pos: dict, px: float, reason: str, qty: float | None = None) -> dict:
        qty = qty or pos["quantity"]
        px = px * (1 - self.p["paper_slippage_pct"] / 100)
        proceeds = px * qty
        pnl = (px - pos["entry_price"]) * qty
        self.s["account"]["cash"] += proceeds
        self.s["account"]["realized_pnl"] += pnl
        risk_per_share = pos["entry_price"] - pos["initial_stop"]
        trade = {"id": _id(), "position_id": pos["id"], "symbol": pos["symbol"], "quantity": qty, "entry_price": pos["entry_price"],
                 "exit_price": round(px, 4), "entry_date": pos["entry_date"], "exit_date": str(dt.date.today()),
                 "exit_at_utc": now_utc().isoformat(timespec="seconds"), "pnl": round(pnl, 2),
                 "pnl_pct": round(100 * (px / pos["entry_price"] - 1), 2),
                 "r_multiple": round((px - pos["entry_price"]) / risk_per_share, 2) if risk_per_share > 0 else None,
                 "reason": reason, "strategy": pos["strategy"], "score": pos["score"], "partial": qty < pos["quantity"]}
        self.s["trades"].append(trade)
        pos["quantity"] = round(pos["quantity"] - qty, 6)
        if pos["quantity"] <= 1e-6:
            self.s["positions"] = [p for p in self.s["positions"] if p["id"] != pos["id"]]
        return trade

    def monitor(self) -> list[dict]:
        """Apply exit rules to every open position. Returns events for alerts."""
        events: list[dict] = []
        today = dt.date.today()
        for pos in list(self.s["positions"]):
            sym = pos["symbol"]
            q = latest_quote(sym)
            px = q.get("price")
            if px is None:
                pos["notes"].append(f"{now_utc().isoformat()} quote unavailable")
                continue
            pos["last_price"] = round(px, 4)
            # Use the intraday range since entry to detect touches (paper realism)
            hi, lo = q.get("day_high") or px, q.get("day_low") or px
            bars = intraday_bars(sym, 2)
            if bars is not None and len(bars):
                try:
                    since = bars[bars.index >= pos["entry_at_utc"]] if pos["entry_date"] == str(today) else bars[bars.index.date == today]
                    if len(since):
                        hi, lo = float(since["high"].max()), float(since["low"].min())
                except Exception:
                    pass
            held = trading_days_between(dt.date.fromisoformat(pos["entry_date"]), today)

            # 1 hard stop (gap-through fills at the open if worse)
            if lo <= pos["stop"]:
                fill = min(pos["stop"], px) if q.get("prev_close") and px < pos["stop"] else pos["stop"]
                t = self.close(pos, fill, "stop_hit")
                events.append({"type": "stop_hit", "symbol": sym, "trade": t, "position": pos})
                continue
            # 2 target 2 → full exit
            if hi >= pos["target_2"]:
                t = self.close(pos, max(pos["target_2"], min(px, hi)), "target_2_hit")
                events.append({"type": "target_2_hit", "symbol": sym, "trade": t, "position": pos})
                continue
            # 3 target 1 → take partial (50%), move stop to breakeven
            if hi >= pos["target_1"] and not pos["t1_hit"]:
                half = round(pos["quantity"] / 2, 4)
                t = self.close(pos, pos["target_1"], "target_1_partial", qty=half)
                pos["t1_hit"] = True
                pos["stop"] = max(pos["stop"], pos["entry_price"])
                pos["notes"].append(f"{today} T1 partial, stop moved to breakeven {pos['stop']}")
                events.append({"type": "target_1_partial", "symbol": sym, "trade": t, "position": pos})
            # 4 time stop: no progress within the setup window
            if held >= pos["setup_window_trading_days"] and not pos["t1_hit"] and px < pos["entry_price"] * 1.02:
                t = self.close(pos, px, "time_stop_no_progress")
                events.append({"type": "time_stop", "symbol": sym, "trade": t, "position": pos})
                continue
            if held >= pos["max_hold_trading_days"]:
                t = self.close(pos, px, "max_hold_reached")
                events.append({"type": "time_stop", "symbol": sym, "trade": t, "position": pos})
                continue
            # 5 earnings guard: exit the session before a report
            nxt, _, _ = next_earnings_date(sym)
            if nxt and trading_days_between(today, nxt) <= 1:
                t = self.close(pos, px, "exit_before_earnings")
                events.append({"type": "exit_before_earnings", "symbol": sym, "trade": t, "position": pos})
                continue
            # 6 trail: once +2R, raise stop to +1R (lock profit, never widen)
            r = pos["entry_price"] - pos["initial_stop"]
            if r > 0 and px >= pos["entry_price"] + 2 * r:
                new_stop = round(pos["entry_price"] + r, 2)
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    pos["notes"].append(f"{today} stop trailed to +1R {new_stop}")
                    events.append({"type": "stop_trailed", "symbol": sym, "position": pos})
        self.s["last_monitor"] = now_utc().isoformat(timespec="seconds")
        return events

    def mark_to_market(self) -> dict:
        mv = 0.0
        for pos in self.s["positions"]:
            q = latest_quote(pos["symbol"])
            if q.get("price"):
                pos["last_price"] = round(q["price"], 4)
            mv += pos["quantity"] * pos.get("last_price", pos["entry_price"])
        equity = self.s["account"]["cash"] + mv
        today = str(dt.date.today())
        curve = self.s["account"]["equity_curve"]
        if curve and curve[-1]["date"] == today:
            curve[-1].update(equity=round(equity, 2), cash=round(self.s["account"]["cash"], 2))
        else:
            curve.append({"date": today, "equity": round(equity, 2), "cash": round(self.s["account"]["cash"], 2)})
        self.s["account"]["equity_curve"] = curve[-800:]
        return {"equity": equity, "market_value": mv, "cash": self.s["account"]["cash"]}

    def reset_capital(self, capital: float) -> None:
        """User set a new trading capital: flatten paper book and restart."""
        self.s["positions"], self.s["orders"], self.s["trades"], self.s["pending_proposals"] = [], [], [], []
        self.s["account"].update(cash=float(capital), starting_capital=float(capital), realized_pnl=0.0,
                                 capital_set_at=now_utc().isoformat(timespec="seconds"), equity_curve=[])
        self.s["settings"]["trading_capital"] = float(capital)
        self.s["fingerprints"] = []
