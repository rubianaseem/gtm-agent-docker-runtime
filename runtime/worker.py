"""The worker: poll -> screen -> enrich -> score -> route -> plan -> (human approves) -> execute.

Container behaviour:
- Runs as PID 1 via exec-form ENTRYPOINT, so SIGTERM from `docker stop` reaches it.
- SIGTERM/SIGINT set a stop flag. The worker finishes the step it is in (or wakes
  from a backoff sleep), commits the checkpoint, writes a clean-stop marker and
  exits 0 well inside the compose stop_grace_period.
- After SIGKILL, OOM or a host restart it resumes from the last committed step.
  Writes that were in flight are re-sent with the same Idempotency-Key, so the
  CRM applies each write once.
- A heartbeat file feeds the HEALTHCHECK.
"""
from __future__ import annotations

import csv
import io
import json
import os
import signal
import threading
import time
from pathlib import Path

from . import approval, boundary, jsonlog, model, planner, router
from .config import Settings, read_secret
from .http_client import CrmClient, HttpError, RetriesExhausted
from .state import STEPS, StateStore, now_iso, sha256_hex

ACTOR = "agent:worker"


class Worker:
    def __init__(self, settings: Settings, token: str | None = None, api_key: str | None = None,
                 install_signals: bool = True):
        self.s = settings
        self.stop = threading.Event()
        self.store = StateStore(settings.state_dir)
        self.rules = router.load_rules(settings.config_dir)
        self.client = CrmClient(settings.crm_base_url, token if token is not None else read_secret("CRM_TOKEN"),
                                settings, stop_event=self.stop)
        self.api_key = api_key if api_key is not None else read_secret("ANTHROPIC_API_KEY", required=False)
        self.model_stats: dict = {}
        self.cycle = 0
        self.counters = {"steps": 0, "executed_ops": 0, "replayed_ops": 0, "stale": 0, "errors": 0}
        if install_signals:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)

    # ------------------------------------------------------------ lifecycle
    def _on_signal(self, signum, _frame):
        if not self.stop.is_set():
            jsonlog.log("info", "shutdown_requested", signal=signal.Signals(signum).name)
        self.stop.set()

    def heartbeat(self, status: str):
        hb = {"ts": time.time(), "iso": now_iso(), "worker_id": self.s.worker_id, "pid": os.getpid(),
              "status": status, "cycle": self.cycle}
        p = self.s.state_dir / "heartbeat.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(hb))
        tmp.replace(p)

    def _fault(self, lead_id: str, point: str):
        tag = f"{lead_id}:{point}"
        if self.s.fault_kill_at == tag:
            jsonlog.log("warning", "fault_injected_sigkill", at=tag)
            os.kill(os.getpid(), signal.SIGKILL)
        if self.s.fault_term_at == tag:
            jsonlog.log("warning", "fault_injected_sigterm", at=tag)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)  # let the handler run

    def start(self):
        torn = self.store.repair_audit_tail()
        if torn is not None:
            jsonlog.log("warning", "audit_torn_tail_quarantined", chars=len(torn))
            self.store.audit("audit_torn_tail_quarantined", ACTOR, chars=len(torn), moved_to="audit.torn.jsonl")
        last = self.store.get_meta("run_state")
        if last and last.get("state") == "running":
            self.store.audit("recovered_after_unclean_stop", ACTOR, previous_pid=last.get("pid"),
                             previous_started=last.get("started"))
            jsonlog.log("warning", "recovered_after_unclean_stop", previous_pid=last.get("pid"))
        self.store.set_meta("run_state", {"state": "running", "pid": os.getpid(), "started": now_iso()})
        self.store.audit("worker_started", ACTOR, worker_id=self.s.worker_id, summary_mode=
                         "claude" if self.api_key else "template")
        self.heartbeat("starting")

    def finish(self, reason: str):
        self.store.set_meta("run_state", {"state": "stopped_clean", "pid": os.getpid(), "stopped": now_iso(),
                                          "reason": reason})
        self.store.audit("worker_stopped", ACTOR, reason=reason, counters=self.counters,
                         http=self.client.stats)
        self.heartbeat("stopped")
        jsonlog.log("info", "worker_stopped", reason=reason, **self.counters)
        self.store.close()

    # --------------------------------------------------------------- ingest
    def ingest(self) -> int:
        try:
            leads = self.client.request("GET", "/crm/leads")["results"]
        except HttpError as e:
            self.counters["errors"] += 1
            jsonlog.log("error", "poll_failed", status=e.status, attempts=e.attempts)
            return 0
        new = 0
        for lead in leads:
            lid, ver = lead["lead_id"], int(lead["version"])
            content = {k: v for k, v in lead.items() if k != "version"}
            chash = sha256_hex(content)
            cur = self.store.get_lead(lid)
            if cur is None:
                self.store.save_lead(lid, ver, chash, "fetched", "in_progress", {"raw": content})
                self.store.audit("lead_ingested", ACTOR, lid, crm_version=ver, content_hash=chash)
                new += 1
            elif cur["crm_version"] == ver:
                continue
            elif cur["status"] == "executing":
                # Our own write may have committed before a crash recorded it. Do not re-plan:
                # the executor re-sends in-flight ops with the same Idempotency-Key, and
                # If-Match still refuses the write if someone else changed the record.
                continue
            elif cur["data"].get("own_write_version") == ver:
                self.store.save_lead(lid, ver, chash, cur["step"], cur["status"], cur["data"], cur["plan_hash"])
            elif cur["status"] == "executed":
                self.store.save_lead(lid, ver, chash, cur["step"], cur["status"], cur["data"], cur["plan_hash"])
                self.store.audit("lead_changed_after_execution", ACTOR, lid, crm_version=ver,
                                 note="recorded only; an approved plan already ran")
            else:
                self.store.save_lead(lid, ver, chash, "fetched", "in_progress",
                                     {"raw": content, "superseded_plan": cur["plan_hash"]})
                self.store.audit("lead_changed_replan", ACTOR, lid, old_version=cur["crm_version"],
                                 new_version=ver, voided_plan=cur["plan_hash"])
                new += 1
        return new

    # ---------------------------------------------------------------- steps
    def _duplicate_of(self, lead_id: str, raw: dict) -> str | None:
        email = (raw.get("email") or "").strip().lower()
        if not email:
            return None
        for other in self.store.all_leads():
            o = other["data"].get("raw", {})
            if other["lead_id"] < lead_id and (o.get("email") or "").strip().lower() == email:
                return other["lead_id"]
        return None

    def advance(self, lead: dict) -> bool:
        """Run the next step for one lead. Returns True if a step ran."""
        lid, step, data = lead["lead_id"], lead["step"], dict(lead["data"])
        raw = data["raw"]
        nxt = STEPS[STEPS.index(step) + 1] if step in STEPS[:-1] else None
        if nxt is None:
            return False
        if nxt == "screened":
            data["screen"] = boundary.screen({k: raw.get(k) for k in ("message", "title", "company")})
            if data["screen"]["flags"]:
                self.store.audit("untrusted_text_quarantined", ACTOR, lid, flags=data["screen"]["flags"])
        elif nxt == "enriched":
            norm = router.normalise(raw, self.rules)
            acct = None
            if norm["email_domain"] and not norm["free_mail"]:
                try:
                    acct = self.client.request("GET", f"/crm/accounts?domain={norm['email_domain']}")["result"]
                except RetriesExhausted as e:
                    self.counters["errors"] += 1
                    jsonlog.log("warning", "enrich_deferred", lead_id=lid, status=e.status)
                    return False  # stays at 'screened'; retried next cycle
            data["enriched"] = router.enrich(norm, acct)
        elif nxt == "scored":
            data["scored"] = router.score(data["enriched"], self.rules)
        elif nxt == "routed":
            data["routed"] = router.route(data["enriched"], data["scored"], self.rules,
                                          duplicate_of=self._duplicate_of(lid, raw))
        elif nxt == "planned":
            text, src = model.summarise(data["screen"]["clean"], data["enriched"], data["scored"], self.rules,
                                        self.api_key, self.model_stats)
            plan = planner.build_plan(raw, lead["crm_version"], data["routed"], data["scored"], text, src,
                                      data["screen"]["flags"], self.rules)
            data["plan"] = plan
            status = "pending_approval" if plan["executable"] else (
                "needs_review" if plan["blocking"] else "closed_no_action")
            self._write_review(plan)
            self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], nxt, status, data, plan["plan_hash"])
            self.write_review_csv()
            self.store.audit("plan_proposed", ACTOR, lid, plan_hash=plan["plan_hash"], decision=plan["decision"],
                             rule=plan["rule"], owner_id=plan["owner_id"], executable=plan["executable"],
                             blocking=plan["blocking"], warnings=plan["warnings"])
            self.counters["steps"] += 1
            self._fault(lid, nxt)
            return True
        self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], nxt, "in_progress", data)
        self.counters["steps"] += 1
        jsonlog.log("debug", "step_done", lead_id=lid, step=nxt)
        self._fault(lid, nxt)
        return True

    def _write_review(self, plan: dict):
        q = self.s.state_dir / "review_queue"
        p = q / f"{plan['lead_id']}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan, indent=2, sort_keys=True))
        tmp.replace(p)

    def write_review_csv(self):
        rows = []
        for lead in self.store.all_leads():
            plan = lead["data"].get("plan")
            if plan:
                rows.append([lead["lead_id"], lead["status"], plan["decision"], plan["rule"], plan["owner_id"] or "",
                             plan["score"], ";".join(plan["blocking"]), ";".join(plan["warnings"]),
                             plan["plan_hash"]])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["lead_id", "status", "decision", "rule", "owner_id", "score", "blocking", "warnings", "plan_hash"])
        w.writerows(rows)
        (self.s.state_dir / "review_queue.csv").write_text(buf.getvalue())

    # -------------------------------------------------------------- execute
    def execute(self, lead: dict) -> bool:
        lid = lead["lead_id"]
        plan = lead["data"]["plan"]
        ok, reason, rec = approval.validate(self.s.state_dir, self.s.config_dir, plan)
        if not ok:
            if reason != "no_approval" and lead["data"].get("last_refusal") != reason:
                data = dict(lead["data"], last_refusal=reason)
                self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], lead["step"], lead["status"],
                                     data, lead["plan_hash"])
                self.store.audit("approval_refused", ACTOR, lid, reason=reason, plan_hash=plan["plan_hash"])
                jsonlog.log("warning", "approval_refused", lead_id=lid, reason=reason)
            return False
        if lead["status"] != "executing":
            self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], lead["step"], "executing",
                                 lead["data"], lead["plan_hash"])
            self.store.audit("execution_started", ACTOR, lid, plan_hash=plan["plan_hash"], approver=rec["approver"])
        data = dict(lead["data"])
        for i, op in enumerate(plan["ops"]):
            if self.stop.is_set():
                jsonlog.log("info", "execution_paused_for_shutdown", lead_id=lid, next_op=i)
                return True
            key = sha256_hex(f"{plan['plan_hash']}:{i}")[:40]
            prior = self.store.get_op(key)
            if prior and prior["status"] == "done":
                continue
            attempts = (prior["attempts"] if prior else 0) + 1
            if prior:
                self.counters["replayed_ops"] += 1
                jsonlog.log("info", "op_resend_same_key", lead_id=lid, op=op["op"], op_index=i)
            self.store.put_op(key, lid, plan["plan_hash"], i, op["op"], "inflight", attempts)
            try:
                resp = self._send_op(op, key, plan["crm_version"] if op["op"] == "update_lead" else None)
            except HttpError as e:
                if e.status == 412:
                    self.store.put_op(key, lid, plan["plan_hash"], i, op["op"], "stale", attempts, e.body)
                    self.counters["stale"] += 1
                    self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], lead["step"],
                                         "stale_needs_replan", data, lead["plan_hash"])
                    self.store.audit("stale_record_refused", ACTOR, lid, plan_version=plan["crm_version"],
                                     current_version=(e.body or {}).get("current_version"),
                                     note="record changed after approval; approval void, lead will be re-planned")
                    return True
                self.counters["errors"] += 1
                self.store.put_op(key, lid, plan["plan_hash"], i, op["op"], "failed_retry_later", attempts,
                                  {"status": e.status})
                self.store.audit("op_failed_retry_later", ACTOR, lid, op=op["op"], status=e.status,
                                 attempts=e.attempts)
                return False  # parked, not progress: --once exits, the loop retries next poll
            self._fault(lid, f"op{i}_sent")  # a crash here = write committed, ack not recorded
            self.store.put_op(key, lid, plan["plan_hash"], i, op["op"], "done", attempts, resp)
            if resp.get("replayed"):
                self.store.audit("op_replay_deduplicated", ACTOR, lid, op=op["op"], idem_key=key)
            else:
                self.counters["executed_ops"] += 1
                self.store.audit("op_applied_mock", ACTOR, lid, op=op["op"], idem_key=key)
            if op["op"] == "update_lead" and "version" in resp:
                data["own_write_version"] = resp["version"]
                self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], lead["step"], "executing",
                                     data, lead["plan_hash"])
        self.store.save_lead(lid, lead["crm_version"], lead["content_hash"], lead["step"], "executed", data,
                             lead["plan_hash"])
        self.store.audit("plan_executed_mock", ACTOR, lid, plan_hash=plan["plan_hash"], ops=len(plan["ops"]))
        return True

    def _send_op(self, op: dict, key: str, if_match: int | None) -> dict:
        if op["op"] == "update_lead":
            return self.client.request("PATCH", f"/crm/leads/{op['lead_id']}", {"set": op["set"]}, idem_key=key,
                                       if_match=if_match)
        if op["op"] == "create_task":
            return self.client.request("POST", "/crm/tasks", {k: op[k] for k in ("lead_id", "owner_id", "subject",
                                                                                 "body", "due")}, idem_key=key)
        if op["op"] == "slack_notify":
            return self.client.request("POST", "/slack/chat.postMessage",
                                       {"channel": op["channel"], "text": op["text"], "lead_id": op.get("lead_id", "")},
                                       idem_key=key)
        raise ValueError(f"unknown op {op['op']}")

    # ----------------------------------------------------------------- loop
    def run_cycle(self) -> bool:
        self.cycle += 1
        self.heartbeat("running")
        progress = self.ingest() > 0
        for lead in self.store.all_leads():
            if self.stop.is_set():
                break
            if lead["status"] == "stale_needs_replan":
                try:
                    fresh = self.client.request("GET", f"/crm/leads/{lead['lead_id']}")
                except HttpError:
                    continue
                content = {k: v for k, v in fresh.items() if k != "version"}
                self.store.save_lead(lead["lead_id"], int(fresh["version"]), sha256_hex(content), "fetched",
                                     "in_progress", {"raw": content, "superseded_plan": lead["plan_hash"]})
                self.store.audit("stale_lead_reread_for_replan", ACTOR, lead["lead_id"],
                                 new_version=int(fresh["version"]), voided_plan=lead["plan_hash"])
                lead = self.store.get_lead(lead["lead_id"])
                progress = True
            while not self.stop.is_set() and lead["step"] != "planned":
                if not self.advance(lead):
                    break
                progress = True
                lead = self.store.get_lead(lead["lead_id"])
            if self.stop.is_set():
                break
            if lead["status"] in ("pending_approval", "executing"):
                progress = self.execute(lead) or progress
            self.heartbeat("running")
        if progress:
            self.write_review_csv()  # refresh status column after executions
        return progress

    def run(self, once: bool = False) -> int:
        self.start()
        reason = "stop_signal"
        try:
            while not self.stop.is_set():
                progressed = self.run_cycle()
                if once and not progressed:
                    reason = "idle_once"
                    break
                if not once:
                    self.stop.wait(self.s.poll_interval_s * self.s.sleep_scale)
        except Exception as e:  # unexpected: record and exit non-zero so the orchestrator restarts us
            jsonlog.log("error", "worker_crashed", error=type(e).__name__, detail=str(e)[:200])
            self.store.audit("worker_crashed", ACTOR, error=type(e).__name__)
            self.heartbeat("fatal")
            return 2
        self.finish(reason)
        return 0
