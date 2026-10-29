"""Persistent state.

Two back-ends with the same interface:
  * LocalStore  – data/state.json on disk (default; fine for a single server).
  * GitHubStore – the same JSON committed to a GitHub repo through the
                  Contents API. Lets the Streamlit site and the GitHub
                  Actions scheduler share one state without a database.

Select with env/secrets: WSO_STORE=github plus GH_TOKEN, GH_REPO (owner/name),
optional GH_BRANCH (default main) and GH_STATE_PATH (default data/state.json).
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import os
import threading
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LOCAL_STATE = ROOT / "data" / "state.json"
_LOCK = threading.Lock()


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def default_state() -> dict:
    return {
        "schema_version": 3,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "settings": {
            "trading_capital": 10_000.0,
            "paper_mode": True,                 # always True in this build
            "human_approval": True,             # orders wait for a human click
            "notifications_enabled": True,
            "emails": [],
            "kill_switch": False,
            "auto_run_cycle": True,             # scheduler may run the pipeline
            "policy_overrides": {},
            "extra_symbols": [],
            "excluded_symbols": [],
            "timezone": "America/New_York",
        },
        "account": {
            "cash": 10_000.0,
            "starting_capital": 10_000.0,
            "capital_set_at": utcnow(),
            "equity_curve": [],                 # [{date, equity, cash}]
            "realized_pnl": 0.0,
        },
        "positions": [],        # open positions
        "orders": [],           # every order ever (paper)
        "trades": [],           # closed round-trips
        "pending_proposals": [],
        "watchlist": [],
        "decision_log": [],
        "alerts": [],           # emails sent
        "regime": None,
        "last_cycle": None,
        "last_monitor": None,
        "reports": {"daily": [], "weekly": []},
        "fingerprints": [],     # order fingerprints (duplicate guard)
    }


def _migrate(state: dict) -> dict:
    base = default_state()
    for k, v in base.items():
        if k not in state:
            state[k] = v
    for k, v in base["settings"].items():
        state["settings"].setdefault(k, v)
    for k, v in base["account"].items():
        state["account"].setdefault(k, v)
    return state


class LocalStore:
    name = "local"

    def __init__(self, path: Path | str = LOCAL_STATE):
        self.path = Path(path)

    def load(self) -> dict:
        with _LOCK:
            if not self.path.exists():
                st = default_state()
                self._write(st)
                return st
            with open(self.path) as f:
                return _migrate(json.load(f))

    def save(self, state: dict) -> None:
        with _LOCK:
            state["updated_at"] = utcnow()
            self._write(state)

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, default=str)
        tmp.replace(self.path)

    def describe(self) -> str:
        return f"Local JSON file ({self.path})"


class GitHubStore:
    name = "github"

    def __init__(self, token: str, repo: str, branch: str = "main", path: str = "data/state.json"):
        self.token, self.repo, self.branch, self.path = token, repo, branch, path
        self._sha: str | None = None
        self.api = f"https://api.github.com/repos/{repo}/contents/{path}"
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}

    def load(self) -> dict:
        r = requests.get(self.api, headers=self.h, params={"ref": self.branch}, timeout=30)
        if r.status_code == 404:
            st = default_state()
            self.save(st)
            return st
        r.raise_for_status()
        j = r.json()
        self._sha = j["sha"]
        return _migrate(json.loads(base64.b64decode(j["content"]).decode()))

    def save(self, state: dict) -> None:
        state["updated_at"] = utcnow()
        body = base64.b64encode(json.dumps(state, indent=1, default=str).encode()).decode()
        payload = {"message": f"wso: state update {state['updated_at']}", "content": body, "branch": self.branch}
        if self._sha is None:
            r = requests.get(self.api, headers=self.h, params={"ref": self.branch}, timeout=30)
            if r.status_code == 200:
                self._sha = r.json()["sha"]
        if self._sha:
            payload["sha"] = self._sha
        r = requests.put(self.api, headers=self.h, json=payload, timeout=30)
        if r.status_code == 409:  # sha conflict -> refresh and retry once
            self._sha = requests.get(self.api, headers=self.h, params={"ref": self.branch}, timeout=30).json()["sha"]
            payload["sha"] = self._sha
            r = requests.put(self.api, headers=self.h, json=payload, timeout=30)
        r.raise_for_status()
        self._sha = r.json()["content"]["sha"]

    def describe(self) -> str:
        return f"GitHub repo {self.repo} ({self.path}@{self.branch})"


def _secret(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v:
        return v
    try:  # Streamlit secrets when running inside the app
        import streamlit as st  # noqa
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def get_store():
    if _secret("WSO_STORE", "local").lower() == "github" and _secret("GH_TOKEN") and _secret("GH_REPO"):
        return GitHubStore(_secret("GH_TOKEN"), _secret("GH_REPO"), _secret("GH_BRANCH", "main"),
                           _secret("GH_STATE_PATH", "data/state.json"))
    return LocalStore(_secret("WSO_STATE_PATH", str(LOCAL_STATE)))


def snapshot(state: dict) -> dict:
    return copy.deepcopy(state)
