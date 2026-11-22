"""Mock CRM + mock Slack over HTTP (standard library only).

This is a local fake for demos, tests and the compose stack. It is not a
HubSpot or Salesforce client. It mimics the behaviours a real API has that
break naive agents: bearer auth, record versions (If-Match -> 412), idempotency
keys, 429 with Retry-After, 5xx, and lost responses after a write committed.

Admin endpoints under /_admin exist only so tests can inject faults and count
writes; a real CRM has nothing like them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import jsonlog
from .config import REPO_ROOT, read_secret


class MockState:
    def __init__(self, leads_csv: Path, accounts_csv: Path):
        self.lock = threading.Lock()
        self.leads = {}
        with open(leads_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row["version"] = 1
                self.leads[row["lead_id"]] = row
        self.accounts = {}
        with open(accounts_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row["named_account"] = row.get("named_account", "").lower() == "true"
                row["open_opportunity"] = row.get("open_opportunity", "").lower() == "true"
                self.accounts[row["domain"].lower()] = row
        self.tasks, self.slack = [], []
        self.idem = {}           # idempotency key -> stored response
        self.effects = []        # every applied write: {"effect": hash, "op": ..., "lead_id": ...}
        self.faults = {"rate_limit_next": 0, "retry_after": 1, "error_next": 0, "drop_after_commit_next": 0,
                       "latency_ms": 0, "fail_paths": [],
                       "bump_before_patch": []}  # simulates a rep editing the record mid-flight
        self.requests = 0
        self.unauthorised = 0
        self.auth_headers_seen = []

    def effect(self, op: str, lead_id: str, payload: dict):
        h = hashlib.sha256(json.dumps([op, lead_id, payload], sort_keys=True).encode()).hexdigest()[:16]
        self.effects.append({"effect": h, "op": op, "lead_id": lead_id})

    def summary(self):
        counts = {}
        for e in self.effects:
            counts[e["effect"]] = counts.get(e["effect"], 0) + 1
        return {"requests": self.requests, "writes": len(self.effects),
                "duplicate_effects": sum(c - 1 for c in counts.values() if c > 1),
                "tasks": len(self.tasks), "slack_messages": len(self.slack), "unauthorised": self.unauthorised,
                "effects": self.effects, "leads": self.leads, "slack": self.slack, "tasks_list": self.tasks}


def make_handler(state: MockState, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mock-crm/1.0"

        def log_message(self, fmt, *args):  # quiet; JSON logs only
            return

        def _send(self, code: int, body: dict, headers: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode())
            except ValueError:
                return {}

        def _gate(self, path: str) -> bool:
            """Auth + injected faults. Returns False if a response was already sent."""
            with state.lock:
                state.requests += 1
                f = state.faults
                if f["latency_ms"]:
                    time.sleep(f["latency_ms"] / 1000)
                auth = self.headers.get("Authorization", "")
                state.auth_headers_seen.append(bool(auth))
                if auth != f"Bearer {token}":
                    state.unauthorised += 1
                    self._send(401, {"error": "unauthorised"})
                    return False
                if f["rate_limit_next"] > 0:
                    f["rate_limit_next"] -= 1
                    self._send(429, {"error": "rate limited"}, {"Retry-After": str(f["retry_after"])})
                    return False
                if f["error_next"] > 0 and (not f["fail_paths"] or any(path.startswith(p) for p in f["fail_paths"])):
                    f["error_next"] -= 1
                    self._send(503, {"error": "unavailable"})
                    return False
            return True

        def _drop_if_injected(self) -> bool:
            with state.lock:
                if state.faults["drop_after_commit_next"] > 0:
                    state.faults["drop_after_commit_next"] -= 1
                    self.close_connection = True
                    return True
            return False

        # ---------------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                return self._send(200, {"ok": True})
            if u.path == "/_admin/summary":
                with state.lock:
                    return self._send(200, state.summary())
            if not self._gate(u.path):
                return
            with state.lock:
                if u.path == "/crm/leads":
                    return self._send(200, {"results": sorted(state.leads.values(), key=lambda r: r["lead_id"])})
                if u.path.startswith("/crm/leads/"):
                    lead = state.leads.get(u.path.rsplit("/", 1)[1])
                    return self._send(200, lead) if lead else self._send(404, {"error": "not found"})
                if u.path == "/crm/accounts":
                    domain = (parse_qs(u.query).get("domain") or [""])[0].lower()
                    acct = state.accounts.get(domain)
                    return self._send(200, {"result": acct})
            return self._send(404, {"error": "no route"})

        # ------------------------------------------------------- PATCH / POST
        def _idempotent(self, key: str | None):
            if key and key in state.idem:
                code, body = state.idem[key]
                return code, dict(body, replayed=True)
            return None

        def do_PATCH(self):
            u = urlparse(self.path)
            if not self._gate(u.path):
                return
            body = self._body()
            key = self.headers.get("Idempotency-Key")
            with state.lock:
                prior = self._idempotent(key)
                if prior:
                    return self._send(*prior)
                if not u.path.startswith("/crm/leads/"):
                    return self._send(404, {"error": "no route"})
                lead = state.leads.get(u.path.rsplit("/", 1)[1])
                if not lead:
                    return self._send(404, {"error": "not found"})
                if lead["lead_id"] in state.faults["bump_before_patch"]:
                    state.faults["bump_before_patch"].remove(lead["lead_id"])
                    lead.update({"owner_id": "rep.uk.smb.1", "lead_status": "Working"})
                    lead["version"] += 1
                im = self.headers.get("If-Match")
                if im is not None and str(lead["version"]) != im:
                    return self._send(412, {"error": "version mismatch", "current_version": lead["version"]})
                allowed = {"owner_id", "lead_status"}
                changes = {k: v for k, v in (body.get("set") or {}).items() if k in allowed}
                if not changes:
                    return self._send(400, {"error": "no allowed fields"})
                lead.update(changes)
                lead["version"] += 1
                state.effect("update_lead", lead["lead_id"], changes)
                resp = (200, {"lead_id": lead["lead_id"], "version": lead["version"]})
                if key:
                    state.idem[key] = resp
            if self._drop_if_injected():
                return
            self._send(*resp)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/_admin/faults":
                with state.lock:
                    state.faults.update(self._body())
                    return self._send(200, state.faults)
            if u.path.startswith("/_admin/mutate/"):
                with state.lock:
                    lead = state.leads.get(u.path.rsplit("/", 1)[1])
                    lead.update(self._body())
                    lead["version"] += 1
                    return self._send(200, {"version": lead["version"]})
            if not self._gate(u.path):
                return
            body = self._body()
            key = self.headers.get("Idempotency-Key")
            with state.lock:
                prior = self._idempotent(key)
                if prior:
                    return self._send(*prior)
                if u.path == "/crm/tasks":
                    task = {k: body.get(k) for k in ("lead_id", "owner_id", "subject", "body", "due")}
                    task["task_id"] = f"T{len(state.tasks) + 1:04d}"
                    state.tasks.append(task)
                    state.effect("create_task", task["lead_id"], {k: task[k] for k in ("owner_id", "subject", "due")})
                    resp = (201, {"task_id": task["task_id"]})
                elif u.path == "/slack/chat.postMessage":
                    msg = {"channel": body.get("channel"), "text": body.get("text"), "ts": f"{len(state.slack) + 1}.0001"}
                    state.slack.append(msg)
                    state.effect("slack_notify", body.get("lead_id", ""), {"channel": msg["channel"], "text": msg["text"]})
                    resp = (200, {"ok": True, "ts": msg["ts"]})
                else:
                    return self._send(404, {"error": "no route"})
                if key:
                    state.idem[key] = resp
            if self._drop_if_injected():
                return
            self._send(*resp)

    return Handler


def start_in_thread(token: str, leads_csv: Path | None = None, accounts_csv: Path | None = None, port: int = 0):
    state = MockState(leads_csv or REPO_ROOT / "sample_leads.csv", accounts_csv or REPO_ROOT / "sample_accounts.csv")
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, token))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, state


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mock CRM + Slack (local fake, no real systems)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--leads", default=str(REPO_ROOT / "sample_leads.csv"))
    ap.add_argument("--accounts", default=str(REPO_ROOT / "sample_accounts.csv"))
    args = ap.parse_args(argv)
    token = read_secret("CRM_TOKEN")
    state = MockState(Path(args.leads), Path(args.accounts))
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(state, token))
    jsonlog.log("info", "mock_crm_listening", host=args.host, port=args.port, leads=len(state.leads))
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    jsonlog.log("info", "mock_crm_stopped")


if __name__ == "__main__":
    main()
