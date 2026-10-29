"""Command-line jobs for the scheduler (GitHub Actions / cron).

  python -m wso.jobs cycle          # run the full pipeline (market hours only unless --force)
  python -m wso.jobs monitor        # manage open positions / fills / exits
  python -m wso.jobs daily_report   # email the daily performance report
  python -m wso.jobs weekly_report  # email the weekly performance report
  python -m wso.jobs tick           # monitor, then cycle if it is the first run of the day
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from .data import is_market_open, now_ny
from .pipeline import monitor, run_cycle
from .reports import send_report
from .store import get_store


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["cycle", "monitor", "daily_report", "weekly_report", "tick", "show"])
    ap.add_argument("--force", action="store_true", help="ignore the market-hours guard")
    a = ap.parse_args(argv)
    store = get_store()
    state = store.load()
    out = {"job": a.job, "store": store.describe(), "ny_time": now_ny().isoformat(timespec="minutes")}

    if a.job == "show":
        out["settings"] = state["settings"]
        out["positions"] = len(state["positions"])
        out["pending"] = [p["symbol"] for p in state["pending_proposals"] if p["status"] == "pending_approval"]
        print(json.dumps(out, indent=1, default=str))
        return 0

    if a.job in ("cycle", "tick", "monitor") and not a.force and not is_market_open():
        out["skipped"] = "market closed"
        print(json.dumps(out, indent=1))
        return 0

    if a.job == "monitor":
        out["monitor"] = monitor(state)
    elif a.job == "cycle":
        if state["settings"].get("auto_run_cycle", True):
            out["cycle"] = run_cycle(state, progress=lambda m: print("·", m, file=sys.stderr))
        else:
            out["skipped"] = "auto_run_cycle disabled in settings"
    elif a.job == "tick":
        out["monitor"] = monitor(state)
        last = (state.get("last_cycle") or {}).get("started_at_utc", "")
        if state["settings"].get("auto_run_cycle", True) and last[:10] != str(dt.date.today()):
            out["cycle"] = run_cycle(state, progress=lambda m: print("·", m, file=sys.stderr))
    elif a.job in ("daily_report", "weekly_report"):
        kind = a.job.split("_")[0]
        today = str(dt.date.today())
        already = any(r["generated_at_utc"][:10] == today for r in state["reports"][kind])
        if already and not a.force:
            out["skipped"] = f"{kind} report already sent today"
        elif now_ny().weekday() >= 5 and not a.force:
            out["skipped"] = "weekend"
        else:
            if kind == "daily":
                monitor(state)
            rep = send_report(state, kind, force=a.force)
            out["report"] = {k: v for k, v in rep.items() if k != "html"}

    store.save(state)
    print(json.dumps(out, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
