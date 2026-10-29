"""Email notifications: setup alerts (possible entries), fills, targets,
stops, and the daily / weekly performance reports.

Transport is chosen from secrets/env:
  RESEND_API_KEY (+ EMAIL_FROM)            -> Resend HTTPS API
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS (+ EMAIL_FROM) -> SMTP (e.g. Gmail app password)
Nothing is sent when notifications are toggled off or no recipients exist.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .store import utcnow

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match(e.strip()))


def _secret(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v:
        return v
    try:
        import streamlit as st  # noqa
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def transport_status() -> dict:
    if _secret("RESEND_API_KEY"):
        return {"configured": True, "transport": "resend", "from": _secret("EMAIL_FROM", "onboarding@resend.dev")}
    if _secret("SMTP_HOST") and _secret("SMTP_USER") and _secret("SMTP_PASS"):
        return {"configured": True, "transport": "smtp", "from": _secret("EMAIL_FROM", _secret("SMTP_USER"))}
    return {"configured": False, "transport": None, "from": None}


def send_email(to: list[str], subject: str, html: str, text: str | None = None) -> dict:
    ts = transport_status()
    to = [t for t in to if valid_email(t)]
    if not to:
        return {"sent": False, "error": "no valid recipients"}
    if not ts["configured"]:
        return {"sent": False, "error": "email transport not configured (set RESEND_API_KEY or SMTP_* secrets)"}
    text = text or re.sub(r"<[^>]+>", "", html)
    try:
        if ts["transport"] == "resend":
            r = requests.post("https://api.resend.com/emails", timeout=30,
                              headers={"Authorization": f"Bearer {_secret('RESEND_API_KEY')}"},
                              json={"from": ts["from"], "to": to, "subject": subject, "html": html, "text": text})
            if r.status_code >= 300:
                return {"sent": False, "error": f"resend {r.status_code}: {r.text[:200]}"}
            return {"sent": True, "transport": "resend", "id": r.json().get("id")}
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, ts["from"], ", ".join(to)
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        port = int(_secret("SMTP_PORT", "587"))
        if port == 465:
            server = smtplib.SMTP_SSL(_secret("SMTP_HOST"), port, timeout=30)
        else:
            server = smtplib.SMTP(_secret("SMTP_HOST"), port, timeout=30)
            server.starttls()
        server.login(_secret("SMTP_USER"), _secret("SMTP_PASS"))
        server.sendmail(ts["from"], to, msg.as_string())
        server.quit()
        return {"sent": True, "transport": "smtp"}
    except Exception as e:  # never let email failures break trading logic
        return {"sent": False, "error": str(e)[:300]}


# ----------------------------------------------------------------- templates
_STYLE = ("font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1f2937;"
          "max-width:680px;margin:auto")
_TH = "text-align:left;padding:6px 8px;border-bottom:1px solid #e5e7eb;background:#f9fafb"
_TD = "padding:6px 8px;border-bottom:1px solid #f3f4f6"


def _table(headers: list[str], rows: list[list]) -> str:
    h = "".join(f"<th style='{_TH}'>{x}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td style='{_TD}'>{x}</td>" for x in r) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse;width:100%'><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def wrap(title: str, body: str, footer: str = "") -> str:
    return (f"<div style='{_STYLE}'><h2 style='margin:0 0 4px'>{title}</h2>"
            f"<div style='color:#6b7280;font-size:12px;margin-bottom:14px'>Wise Stocks Optionaire · paper trading · {utcnow()} UTC</div>"
            f"{body}<p style='color:#6b7280;font-size:12px;margin-top:18px'>{footer} Rules: limit orders only, stop 20–30% of the "
            "position at technical invalidation, no averaging down, a high score never authorizes an order. "
            "This is a simulation; nothing here is investment advice.</p></div>")


def setup_alert_html(proposals: list[dict], watch: list[dict], regime: dict, approval_on: bool) -> str:
    rows = [[p["symbol"], p["strategy"], f"${p['limit_price']:.2f}", f"${p['stop_price']:.2f} ({100*(1-p['stop_price']/p['limit_price']):.1f}%)",
             f"${p['target_1']:.2f} / ${p['target_2']:.2f}", p["expected_rr"], p["score"], p["decision"],
             f"{p['quantity']} sh ≈ ${p['quantity']*p['limit_price']:.2f}"] for p in proposals]
    body = f"<p>Market regime: <b>{regime.get('regime','?')}</b> (score {regime.get('score','?')}). Leaders: {', '.join(regime.get('leaders', []))}.</p>"
    if proposals:
        body += "<h3>Possible entries" + (" — waiting for your approval on the site" if approval_on else " — auto-submitted as paper limit orders") + "</h3>"
        body += _table(["Ticker", "Setup", "Limit", "Stop", "T1 / T2", "R:R", "Score", "Decision", "Size"], rows)
    else:
        body += "<p>No candidate cleared the order gates (score ≥ 75 and reward:risk ≥ 2.0).</p>"
    if watch:
        body += "<h3>Watchlist (needs a trigger first)</h3>" + _table(
            ["Ticker", "Setup", "Price", "Score", "Why not yet"],
            [[w["symbol"], w["setup"], f"${w['price']:.2f}", w["score"], "; ".join(w["reasons"][:2])] for w in watch[:10]])
    return wrap("Setup alert", body)


def event_html(ev: dict) -> tuple[str, str]:
    t, sym = ev["type"], ev["symbol"]
    pos, tr = ev.get("position", {}), ev.get("trade")
    titles = {"entry_filled": f"Entry filled: {sym}", "stop_hit": f"Stop hit: {sym}", "target_1_partial": f"Target 1 reached: {sym} (half sold)",
              "target_2_hit": f"Target 2 reached: {sym} (closed)", "time_stop": f"Time stop: {sym}",
              "exit_before_earnings": f"Exited before earnings: {sym}", "stop_trailed": f"Stop raised: {sym}"}
    title = titles.get(t, f"{t}: {sym}")
    rows = []
    if tr:
        rows = [["Quantity", tr["quantity"]], ["Entry", f"${tr['entry_price']:.2f}"], ["Exit", f"${tr['exit_price']:.2f}"],
                ["P&L", f"${tr['pnl']:.2f} ({tr['pnl_pct']:+.2f}%, {tr['r_multiple']}R)"], ["Reason", tr["reason"]]]
    elif t == "entry_filled":
        rows = [["Quantity", pos["quantity"]], ["Fill", f"${pos['entry_price']:.2f}"], ["Stop", f"${pos['stop']:.2f}"],
                ["Targets", f"${pos['target_1']:.2f} / ${pos['target_2']:.2f}"], ["Setup", pos["strategy"]]]
    elif t == "stop_trailed":
        rows = [["New stop", f"${pos['stop']:.2f}"], ["Last", f"${pos.get('last_price', 0):.2f}"]]
    return title, wrap(title, _table(["Field", "Value"], rows))


def report_html(kind: str, perf: dict, trades: list[dict], positions: list[dict], decisions: list[dict], regime: dict | None) -> str:
    k = "Daily" if kind == "daily" else "Weekly"
    s = perf
    body = _table(["Metric", "Value"], [
        ["Equity", f"${s['equity']:,.2f}"], ["Cash", f"${s['cash']:,.2f}"],
        [f"{k} P&L", f"${s['period_pnl']:,.2f} ({s['period_pnl_pct']:+.2f}%)"],
        ["Since start", f"${s['total_pnl']:,.2f} ({s['total_return_pct']:+.2f}%)"],
        ["Closed trades (period / total)", f"{s['period_trades']} / {s['total_trades']}"],
        ["Win rate (total)", f"{s['win_rate']:.0f}%"], ["Avg R (total)", s["avg_r"]], ["Max drawdown", f"{s['max_drawdown_pct']:.2f}%"],
        ["Open positions", len(positions)], ["Portfolio heat", f"{s['heat_pct']:.2f}%"]])
    if regime:
        body += f"<p>Regime: <b>{regime.get('regime')}</b> ({regime.get('score')}); leaders {', '.join(regime.get('leaders', []))}.</p>"
    if positions:
        body += "<h3>Open positions</h3>" + _table(["Ticker", "Qty", "Entry", "Last", "Stop", "T1 / T2", "Unrealized"],
            [[p["symbol"], p["quantity"], f"${p['entry_price']:.2f}", f"${p.get('last_price', p['entry_price']):.2f}", f"${p['stop']:.2f}",
              f"${p['target_1']:.2f} / ${p['target_2']:.2f}", f"${(p.get('last_price', p['entry_price'])-p['entry_price'])*p['quantity']:.2f}"] for p in positions])
    if trades:
        body += f"<h3>Closed this {('day' if kind=='daily' else 'week')}</h3>" + _table(["Ticker", "Exit", "P&L", "R", "Reason"],
            [[t["symbol"], t["exit_date"], f"${t['pnl']:.2f}", t["r_multiple"], t["reason"]] for t in trades])
    if decisions:
        body += "<h3>Decisions logged</h3>" + _table(["Time (UTC)", "Ticker", "Decision", "Reason"],
            [[d["at"][:16], d.get("symbol", "—"), d["decision"], d["reason"][:120]] for d in decisions[-15:]])
    return wrap(f"{k} performance report", body)
