# Wise Stocks Optionaire — rule-bound trading agent (Streamlit)

A paper-trading implementation of the **WSO** process: regime → universe screen → specialist analysis →
Critic → deterministic risk engine → human approval (toggle) → paper execution → monitoring → email alerts →
daily / weekly performance reports. Every decision (taken, skipped, exited) is logged against the rule that produced it.

The binding rules come from `docs/WSO_rules_source.md` (the original instruction module) and the refined
policy v2 agreed on 2026-10-29 (`wso/policy_defaults.json`, flattened in `wso/policy.py`).

## What the site does

| Page | Purpose |
|---|---|
| Dashboard | Paper equity, heat, regime, pending proposals (approve / decline), open positions, latest decisions |
| Run pipeline | Runs all 10 gates on demand; shows every candidate with score, decision, stop, targets, R:R, earnings date, bear case |
| Positions & orders | Open positions with manual close / sell-half (logged as user overrides), closed trades, orders, proposal history |
| Watchlist | Names in the 65–74 band or blocked by one hard gate, each with the trigger that would re-qualify it |
| Decision log | Filterable log, downloadable as Markdown/JSON; also the email delivery log |
| Reports | Daily / weekly performance report preview and one-click send |
| Settings | **Trading capital (paper)**, **Human approval ON/OFF**, **Email notifications ON/OFF**, recipient emails (add/remove), kill switch, universe edits, bounded policy editor |
| Rules | The rules in force, score bands, exit rules, scoring weights |

## Rules enforced by code (not prompts)

* Position size by regime: 25 / 20 / 15 / 10 / 0 % of equity (aggressive / growth / balanced / defensive / risk-off); one tier smaller when ATR > 5 % of price; half size for conditional (75–81) scores.
* Stop = technical invalidation, at most 20–30 % of the **position**; hard cap 7.5 % of account per trade. Tighter is always allowed, wider is rejected.
* Portfolio heat ≤ 15 %, gross ≤ 80 %, cash reserve ≥ 20 %, ≤ 4 positions, ≤ 2 per sector/correlated cluster, sector ≤ 40 %.
* Breakers: daily −5 %, weekly −10 %, pause after 3 consecutive losses; kill switch.
* Limit orders only, limit ≤ reference × 1.005, quote ≤ 120 s old, account snapshot ≤ 300 s old, regular hours only, day time-in-force, duplicate fingerprint guard, never add to an existing position.
* Order gate: WSO score ≥ 75 **and** expected reward:risk ≥ 2.0; earnings inside the 15-trading-day window → not tradable; max hold 30 trading days.
* Exits: hard stop; half off at T1 and stop to breakeven; full exit at T2; trail to +1R after +2R; time stop; exit the session before earnings. Never widen a stop, never average down.
* Data integrity: every number is computed from vendor data (yfinance) with an as-of date; missing fields are `None`, never estimated.

## Deploy (Streamlit Community Cloud, free)

1. Fork or use this repo on GitHub.
2. Go to https://share.streamlit.io → **Create app** → pick this repo, branch `main`, file `app.py`.
3. Under **Advanced settings → Secrets**, paste the contents of `.streamlit/secrets.toml.example` with your values (see below).
4. Deploy. First load takes ~1 minute (installs `requirements.txt`).

Any other host that runs `streamlit run app.py` works too (Hugging Face Spaces, Render, a VPS, Docker).

## Secrets

Email (choose one transport):

```toml
# Resend (https://resend.com) — simplest
RESEND_API_KEY = "re_xxx"
EMAIL_FROM = "WSO Agent <alerts@yourdomain.com>"
# or SMTP (Gmail app password etc.)
SMTP_HOST = "smtp.gmail.com"; SMTP_PORT = "587"; SMTP_USER = "you@gmail.com"; SMTP_PASS = "app-password"; EMAIL_FROM = "you@gmail.com"
```

Shared state (recommended once the scheduler is on, so the site and the scheduler see the same book):

```toml
WSO_STORE = "github"
GH_TOKEN = "github_pat_xxx"      # fine-grained PAT, Contents: read & write on this repo
GH_REPO = "your-user/wso-trading-agent"
GH_BRANCH = "main"
```

Without `WSO_STORE = "github"` the app keeps `data/state.json` on its own disk (fine for local use; Streamlit Cloud
resets that file on every reboot).

## Scheduler (daily / weekly reports, monitoring, auto-runs)

`.github/workflows/wso_scheduler.yml` runs on GitHub Actions:

* every 30 min during the US session: monitor positions (fills, stops, targets, time stops, earnings exits) and run the first pipeline cycle of the day;
* ~16:35 ET on trading days: **daily report** email;
* Friday ~16:50 ET: **weekly report** email.

Add the same secrets to the repo (**Settings → Secrets and variables → Actions**): `WSO_STORE`, `GH_TOKEN`, and the email transport keys.
Use **Actions → WSO scheduler → Run workflow** to trigger any job by hand (tick / cycle / monitor / daily_report / weekly_report).

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in what you need
streamlit run app.py
# CLI jobs
python -m wso.jobs cycle --force
python -m wso.jobs monitor --force
python -m wso.jobs daily_report --force
pytest -q
```

## Project layout

```
app.py                  Streamlit console
wso/policy.py           numeric policy + bounds (user instructions)
wso/universe.py         tickers, sectors, correlated clusters
wso/data.py             yfinance data layer (timestamps, never invented values)
wso/regime.py           Gate 2 regime engine
wso/technical.py        indicators, setup detection, stops & targets
wso/scoring.py          12-factor WSO score, penalties, Critic, decision mapping
wso/risk_engine.py      Gate 6 deterministic checks + sizing
wso/paper_broker.py     paper fills and exit rules
wso/pipeline.py         orchestrator, approvals, monitor
wso/notify.py           email transport + templates
wso/reports.py          performance metrics, daily/weekly reports
wso/jobs.py             CLI entry points used by the scheduler
wso/store.py            local / GitHub-backed JSON persistence
tests/                  risk-engine, broker and page smoke tests
docs/                   original rules document (docx + markdown)
```

## Going live later

This build is **paper only**. The execution packet, risk engine, approval stage and logging are broker-agnostic;
a live adapter would replace `PaperBroker.place_limit_buy` / `monitor` with broker calls and keep every gate in front of it.

*Nothing in this repository is investment advice. Simulated results do not include real slippage, halts or gap risk.*
