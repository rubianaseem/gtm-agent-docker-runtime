"""Deterministic naive baseline: what a first "just put the script in a container" version does.

Same routing rules as the runtime (so the comparison isolates runtime behaviour), but:
- no approval: every executable plan is applied immediately
- no Idempotency-Key, no If-Match version check
- retries any failure up to 5 times with a fixed short sleep
- no checkpoint: a restart starts from the top and re-applies everything
- logs the failing request including its headers (a common debugging habit)
A crash is simulated by raising at a named point, then calling run() again.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from runtime import boundary, model, planner, router


class Crash(Exception):
    pass


class NaiveWorker:
    def __init__(self, base_url: str, token: str, config_dir, crash_at: str = ""):
        self.base, self.token, self.crash_at = base_url, token, crash_at
        self.rules = router.load_rules(config_dir)
        self.log: list[str] = []
        self.crashed = False

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        for attempt in range(5):
            try:
                req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read().decode() or "{}")
            except Exception as e:  # noqa: BLE001 - the naive version retries everything
                self.log.append(f"request failed {method} {path} headers={headers} err={type(e).__name__}")
                time.sleep(0.001)
        return {}

    def run(self):
        leads = self._req("GET", "/crm/leads").get("results", [])
        seen = []
        for lead in leads:
            raw = {k: v for k, v in lead.items() if k != "version"}
            scr = boundary.screen({k: raw.get(k) for k in ("message", "title", "company")})
            norm = router.normalise(raw, self.rules)
            acct = None
            if norm["email_domain"] and not norm["free_mail"]:
                acct = self._req("GET", f"/crm/accounts?domain={norm['email_domain']}").get("result")
            enr = router.enrich(norm, acct)
            sc = router.score(enr, self.rules)
            dup = next((s for s in seen if s[1] == (raw.get("email") or "").lower()), None)
            seen.append((raw["lead_id"], (raw.get("email") or "").lower()))
            rt = router.route(enr, sc, self.rules, duplicate_of=dup[0] if dup else None)
            text, src = model.summarise(scr["clean"], enr, sc, self.rules, None, {})
            plan = planner.build_plan(raw, int(lead["version"]), rt, sc, text, src, scr["flags"], self.rules)
            if not plan["executable"]:
                continue
            for i, op in enumerate(plan["ops"]):
                if op["op"] == "update_lead":
                    self._req("PATCH", f"/crm/leads/{op['lead_id']}", {"set": op["set"]})
                elif op["op"] == "create_task":
                    self._req("POST", "/crm/tasks", {k: op[k] for k in ("lead_id", "owner_id", "subject", "body", "due")})
                else:
                    self._req("POST", "/slack/chat.postMessage", {"channel": op["channel"], "text": op["text"],
                                                                 "lead_id": op["lead_id"]})
                if self.crash_at == f"{raw['lead_id']}:op{i}_sent" and not self.crashed:
                    self.crashed = True
                    raise Crash(self.crash_at)
