"""Shared helpers for the failure suite, the demo and tests.

The worker runs as a real subprocess (`python -m runtime run --once`) so that
SIGTERM and SIGKILL behave as they would for PID 1 in a container. The mock CRM
runs in a thread of the harness so the harness can inject faults and count writes.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from runtime import approval, mock_services
from runtime.state import StateStore

ROOT = Path(__file__).resolve().parent.parent


class Env:
    def __init__(self, token: str | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="gtm-docker-rt-"))
        self.state = self.tmp / "state"
        self.secrets = self.tmp / "secrets"
        self.secrets.mkdir()
        self.token = token or ("tok-" + secrets.token_hex(12))
        (self.secrets / "crm_token").write_text(self.token + "\n")
        self.srv, self.mock = mock_services.start_in_thread(self.token)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.logs: list[str] = []

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def env(self, **extra) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "CRM_TOKEN", "FAULT_"))}
        e.update({"CRM_BASE_URL": self.url, "STATE_DIR": str(self.state), "SECRETS_DIR": str(self.secrets),
                  "SLEEP_SCALE": "0.01", "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run_worker(self, timeout: float = 60, **extra) -> subprocess.CompletedProcess:
        p = subprocess.run([sys.executable, "-m", "runtime", "run", "--once"], cwd=ROOT, env=self.env(**extra),
                           capture_output=True, text=True, timeout=timeout)
        self.logs.append(p.stdout + p.stderr)
        return p

    def spawn_worker(self, **extra) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "runtime", "run"], cwd=ROOT, env=self.env(**extra),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def cli(self, *args) -> subprocess.CompletedProcess:
        p = subprocess.run([sys.executable, "-m", "runtime", *args], cwd=ROOT, env=self.env(), capture_output=True,
                           text=True, timeout=30)
        self.logs.append(p.stdout + p.stderr)
        return p

    def faults(self, **f):
        with self.mock.lock:
            self.mock.faults.update(f)

    def mutate(self, lead_id: str, **fields):
        with self.mock.lock:
            lead = self.mock.leads[lead_id]
            lead.update(fields)
            lead["version"] += 1

    def store(self) -> StateStore:
        return StateStore(self.state)

    def plans(self) -> dict:
        st = self.store()
        out = {l["lead_id"]: l["data"].get("plan") for l in st.all_leads()}
        st.close()
        return out

    def statuses(self) -> dict:
        st = self.store()
        out = {l["lead_id"]: l["status"] for l in st.all_leads()}
        st.close()
        return out

    def approve_all(self, approver: str = "rubia.naseem", only: list | None = None) -> list[str]:
        """Test stand-in for a human reviewer. It approves exactly what is in the review queue."""
        done = []
        for lid, plan in self.plans().items():
            if plan and plan["executable"] and (only is None or lid in only):
                approval.write_approval(self.state, ROOT / "config", lid, plan["plan_hash"], approver, 24)
                done.append(lid)
        return done

    def summary(self) -> dict:
        with self.mock.lock:
            return json.loads(json.dumps(self.mock.summary()))

    def secret_leaks(self) -> int:
        """Count files and log captures that contain the token bytes."""
        n = sum(1 for log in self.logs if self.token in log)
        for p in self.state.rglob("*"):
            if p.is_file() and self.token.encode() in p.read_bytes():
                n += 1
        return n


def expected_effects(plans: dict, approved: list[str]) -> int:
    return sum(len(plans[l]["ops"]) for l in approved)
