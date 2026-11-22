"""Failure-injection suite: python -m evals.failure_suite [--json out.json]

Runs every scenario in failure_scenarios_v1.json against the runtime (real
subprocesses, real signals) and, where the comparison is meaningful, against
the naive baseline in evals/naive_worker.py.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from evals.harness import ROOT, Env
from evals.naive_worker import Crash, NaiveWorker
from runtime import approval
from runtime.state import verify_audit

SCEN = json.loads((ROOT / "evals" / "failure_scenarios_v1.json").read_text())
ALL_ENVS: list[Env] = []


def new_env(**kw) -> Env:
    e = Env(**kw)
    ALL_ENVS.append(e)
    return e


def plan_approve_execute(e: Env, **fault_env):
    r1 = e.run_worker()
    approved = e.approve_all()
    r2 = e.run_worker(**fault_env)
    return r1, approved, r2


def audit_events(e: Env, name: str) -> list:
    st = e.store()
    ev = [a for a in st.audit_events() if a.get("event") == name]
    st.close()
    return ev


def naive(e: Env, crash_at: str = "", reruns: int = 1) -> dict:
    w = NaiveWorker(e.url, e.token, ROOT / "config", crash_at=crash_at)
    for _ in range(reruns):
        try:
            w.run()
        except Crash:
            w.run()  # restart from the top: no checkpoint
    s = e.summary()
    leaks = sum(1 for line in w.log if e.token in line)
    return {"writes": s["writes"], "duplicate_effects": s["duplicate_effects"], "unapproved_writes": s["writes"],
            "requests": s["requests"], "log_lines_with_token": leaks}


REF = {}


def reference_hashes() -> dict:
    if not REF:
        e = new_env()
        e.run_worker()
        REF.update({k: v["plan_hash"] for k, v in e.plans().items()})
        REF["_ops"] = sum(len(v["ops"]) for v in e.plans().values() if v["executable"])
    return REF


def rt_metrics(e: Env, approved: list) -> dict:
    s = e.summary()
    ok, n, _ = verify_audit(e.state / "audit.jsonl")
    return {"writes": s["writes"], "duplicate_effects": s["duplicate_effects"],
            "expected_writes": sum(len(e.plans()[l]["ops"]) for l in approved),
            "audit_chain_ok": ok, "audit_records": n}


def f01():
    e = new_env()
    r1, appr, r2 = plan_approve_execute(e)
    m = rt_metrics(e, appr)
    m["unapproved_writes"] = 0 if m["writes"] == m["expected_writes"] else m["writes"] - m["expected_writes"]
    ok = r1.returncode == 0 and r2.returncode == 0 and m["writes"] == m["expected_writes"] and \
        m["duplicate_effects"] == 0 and m["audit_chain_ok"]
    return ok, m, naive(new_env())


def f02():
    ref = reference_hashes()
    e = new_env()
    r1 = e.run_worker(FAULT_KILL_AT="L004:enriched")
    r2 = e.run_worker()
    plans = e.plans()
    mism = sum(1 for k, v in plans.items() if v is None or v["plan_hash"] != ref.get(k))
    lost = len([k for k in ref if not k.startswith("_")]) - len([v for v in plans.values() if v])
    rec = len(audit_events(e, "recovered_after_unclean_stop"))
    ok = r1.returncode == -signal.SIGKILL and r2.returncode == 0 and mism == 0 and lost == 0 and rec == 1
    return ok, {"first_exit": r1.returncode, "plan_hash_mismatches": mism, "lost_leads": lost,
                "recovery_events": rec}, None


def f03():
    m_all, ok_all, base = {}, True, {}
    for point in ("L001:op0_sent", "L002:op2_sent"):
        e = new_env()
        e.run_worker()
        appr = e.approve_all()
        r2 = e.run_worker(FAULT_KILL_AT=point)
        r3 = e.run_worker()
        m = rt_metrics(e, appr)
        m.update(first_exit=r2.returncode, deduplicated_replays=len(audit_events(e, "op_replay_deduplicated")))
        ok_all = ok_all and r2.returncode == -signal.SIGKILL and r3.returncode == 0 and \
            m["duplicate_effects"] == 0 and m["writes"] == m["expected_writes"]
        m_all[point] = m
        b = naive(new_env(), crash_at=point)
        b["extra_writes_vs_clean_run"] = b["writes"] - m["expected_writes"]
        base[point] = b
    return ok_all, m_all, base


def f04():
    e = new_env()
    e.run_worker()
    appr = e.approve_all()
    r2 = e.run_worker(FAULT_TERM_AT="L002:op0_sent")
    st = e.store()
    clean = (st.get_meta("run_state") or {}).get("state")
    pending_after_stop = e.statuses().get("L002")
    st.close()
    r3 = e.run_worker()
    m = rt_metrics(e, appr)
    m.update(first_exit=r2.returncode, run_state_after_sigterm=clean, l002_status_after_sigterm=pending_after_stop)
    ok = r2.returncode == 0 and clean == "stopped_clean" and pending_after_stop == "executing" and \
        r3.returncode == 0 and m["duplicate_effects"] == 0 and m["writes"] == m["expected_writes"]
    return ok, m, None


def _fault_run(fault: dict, naive_fault: dict | None = None):
    e = new_env()
    e.run_worker()
    appr = e.approve_all()
    e.faults(**fault)
    r2 = e.run_worker()
    m = rt_metrics(e, appr)
    log = e.logs[-1]
    m["http_retry_log_lines"] = log.count('"event": "http_retry"')
    m["retry_after_honoured"] = log.count('"retry_after": true')
    ok = r2.returncode == 0 and m["duplicate_effects"] == 0 and m["writes"] == m["expected_writes"]
    b = None
    if naive_fault is not None:
        ne = new_env()
        ne.faults(**naive_fault)
        b = naive(ne)
    return ok, m, b


def f05():
    ok, m, b = _fault_run({"rate_limit_next": 8, "retry_after": 2}, {"rate_limit_next": 8, "retry_after": 2})
    return ok and m["retry_after_honoured"] >= 1, m, b


def f06():
    return _fault_run({"error_next": 3}, {"error_next": 3})


def f07():
    return _fault_run({"drop_after_commit_next": 3}, {"drop_after_commit_next": 3})


def f08():
    e = new_env()
    e.run_worker()
    appr = e.approve_all()
    e.faults(error_next=10_000, fail_paths=["/crm/leads/", "/crm/tasks", "/slack"])
    r2 = e.run_worker()
    during = e.summary()["writes"]
    parked = len(audit_events(e, "op_failed_retry_later"))
    e.faults(error_next=0)
    r3 = e.run_worker()
    m = rt_metrics(e, appr)
    m.update(exit_during_outage=r2.returncode, writes_during_outage=during, ops_parked=parked)
    ok = r2.returncode == 0 and parked >= 1 and r3.returncode == 0 and m["duplicate_effects"] == 0 and \
        m["writes"] == m["expected_writes"]
    return ok, m, None


def f09():
    e = new_env()
    e.run_worker()
    e.approve_all()
    e.faults(bump_before_patch=["L001"])  # the rep's edit lands between our read and our write
    r2 = e.run_worker()
    s = e.summary()
    owner = s["leads"]["L001"]["owner_id"]
    l001_writes = sum(1 for x in s["effects"] if x["lead_id"] == "L001")
    refused = [a for a in audit_events(e, "approval_refused") if a["lead_id"] == "L001"]
    stale = len(audit_events(e, "stale_record_refused"))
    new_plan = e.plans()["L001"]
    m = {"l001_owner_after": owner, "l001_writes": l001_writes, "stale_412_refusals": stale,
         "old_approval_refused": [a["detail"]["reason"] for a in refused],
         "replanned_decision": new_plan["decision"], "replanned_rule": new_plan["rule"],
         "l001_status": e.statuses()["L001"]}
    ok = r2.returncode == 0 and owner == "rep.uk.smb.1" and l001_writes == 0 and stale == 1 and \
        "hash_mismatch" in m["old_approval_refused"]
    return ok, m, None


def f10():
    e = new_env()
    e.run_worker()
    p = e.state / "review_queue" / "L002.json"
    plan = json.loads(p.read_text())
    plan["ops"][0]["set"]["owner_id"] = "rep.uk.ent.1"
    p.write_text(json.dumps(plan))
    from runtime.planner import plan_hash
    approval.write_approval(e.state, ROOT / "config", "L002", plan_hash(plan), "rubia.naseem", 24)
    e.run_worker()
    s = e.summary()
    refused = [a["detail"]["reason"] for a in audit_events(e, "approval_refused") if a["lead_id"] == "L002"]
    m = {"l002_writes": sum(1 for x in s["effects"] if x["lead_id"] == "L002"), "refusals": refused}
    return m["l002_writes"] == 0 and "hash_mismatch" in refused, m, None


def f11():
    e = new_env()
    e.run_worker()
    plan = e.plans()["L002"]
    approval.write_approval(e.state, ROOT / "config", "L002", plan["plan_hash"], "rubia.naseem", 24,
                            now=time.time() - 25 * 3600)
    e.run_worker()
    s = e.summary()
    refused = [a["detail"]["reason"] for a in audit_events(e, "approval_refused")]
    m = {"writes": s["writes"], "refusals": refused}
    return s["writes"] == 0 and "approval_expired" in refused, m, None


def f12():
    e = new_env()
    e.run_worker()
    plan = e.plans()["L002"]
    c = e.cli("approve", "L002", "--hash", plan["plan_hash"], "--approver", "agent:worker")
    forged = {"lead_id": "L002", "plan_hash": plan["plan_hash"], "approver": "agent:worker",
              "approved_at": "2026-11-22T00:00:00Z", "expires_at_epoch": time.time() + 3600}
    (e.state / "approvals" / "L002.json").write_text(json.dumps(forged))
    e.run_worker()
    s = e.summary()
    refused = [a["detail"]["reason"] for a in audit_events(e, "approval_refused")]
    m = {"cli_exit": c.returncode, "cli_output": c.stdout.strip()[:90], "writes": s["writes"], "refusals": refused}
    return c.returncode == 1 and s["writes"] == 0 and any(r.startswith("approver_refused") for r in refused), m, None


def f13():
    e = new_env()
    e.run_worker()
    plan = e.plans()["L002"]
    c = e.cli("approve", "L002", "--hash", plan["plan_hash"][:12], "--approver", "rubia.naseem")
    return c.returncode == 1 and "full 64-character" in c.stdout, {"cli_exit": c.returncode,
                                                                   "cli_output": c.stdout.strip()[:90]}, None


def f14():
    e = new_env()
    (e.secrets / "crm_token").unlink()
    r = e.run_worker()
    s = e.summary()
    m = {"exit": r.returncode, "requests": s["requests"], "message_ok": "startup_refused_missing_secret" in r.stdout}
    return r.returncode == 3 and s["requests"] == 0 and m["message_ok"], m, None


def f15():
    e = new_env()
    wrong = "wrong-" + e.token
    (e.secrets / "crm_token").write_text(wrong)
    r = e.run_worker()
    s = e.summary()
    leak = wrong in r.stdout or wrong in r.stderr
    m = {"exit": r.returncode, "requests": s["requests"], "unauthorised": s["unauthorised"], "writes": s["writes"],
         "token_in_logs": leak}
    ne = new_env()
    nw = NaiveWorker(ne.url, "wrong-" + ne.token, ROOT / "config")
    nw.run()
    ns = ne.summary()
    b = {"requests": ns["requests"], "unauthorised": ns["unauthorised"],
         "log_lines_with_token": sum(1 for l in nw.log if ("wrong-" + ne.token) in l)}
    return r.returncode == 0 and s["requests"] == 1 and s["writes"] == 0 and not leak, m, b


def f16():
    e = new_env()
    _, appr, _ = plan_approve_execute(e)
    s = e.summary()
    plans = e.plans()
    texts = [t["body"] for t in s["tasks_list"]] + [m["text"] for m in s["slack"]]
    bad = [t for t in texts if any(x in t.lower() for x in ("ignore previous", "set owner", "<system>", "lifecycle customer"))]
    m = {"L007_owner": plans["L007"]["owner_id"], "L012_owner": plans["L012"]["owner_id"],
         "L007_warnings": plans["L007"]["warnings"], "L012_warnings": plans["L012"]["warnings"],
         "injected_text_in_outputs": len(bad)}
    ok = plans["L007"]["owner_id"] != "rep.na.ent.1" and plans["L012"]["owner_id"] != "rep.uk.ent.1" and not bad
    return ok, m, None


def f17():
    e = new_env()
    plan_approve_execute(e)
    before = e.summary()["writes"]
    plans_before = len(audit_events(e, "plan_proposed"))
    e.run_worker()
    m = {"new_writes": e.summary()["writes"] - before,
         "new_plans": len(audit_events(e, "plan_proposed")) - plans_before}
    ne = new_env()
    naive(ne, reruns=2)
    ns = ne.summary()
    b = {"duplicate_effects": ns["duplicate_effects"], "writes": ns["writes"]}
    return m["new_writes"] == 0 and m["new_plans"] == 0, m, b


def f18():
    e = new_env()
    e.run_worker()
    with (e.state / "audit.jsonl").open("a") as f:
        f.write('{"ts": "2026-11-22T10:00:00Z", "event": "plan_pro')
    bad_ok, _, bad_msg = verify_audit(e.state / "audit.jsonl")
    e.run_worker()
    ok, n, msg = verify_audit(e.state / "audit.jsonl")
    torn = (e.state / "audit.torn.jsonl").exists()
    m = {"before_repair": bad_msg, "after": msg, "torn_file": torn}
    return (not bad_ok) and ok and torn, m, None


def f19():
    e = new_env()
    e.run_worker()
    hb = e.state / "heartbeat.json"
    res = {}

    def probe():
        return subprocess.run([sys.executable, "-m", "runtime", "healthcheck"], cwd=ROOT, env=e.env(),
                              capture_output=True, text=True).returncode
    data = json.loads(hb.read_text())
    data.update(ts=time.time(), status="running")
    hb.write_text(json.dumps(data))
    res["fresh"] = probe()
    data.update(ts=time.time() - 120)
    hb.write_text(json.dumps(data))
    res["stale"] = probe()
    data.update(ts=time.time(), status="fatal")
    hb.write_text(json.dumps(data))
    res["fatal"] = probe()
    data.update(ts=time.time(), status="stopped")
    hb.write_text(json.dumps(data))
    res["stopped"] = probe()
    hb.unlink()
    res["missing"] = probe()
    return res == {"fresh": 0, "stale": 1, "fatal": 1, "stopped": 1, "missing": 1}, res, None


def f20():
    e = new_env()
    e.run_worker()
    old = e.plans()["L002"]
    approval.write_approval(e.state, ROOT / "config", "L002", old["plan_hash"], "rubia.naseem", 24)
    e.mutate("L002", employees="1500", message="We are now 1500 people, please send pricing.")
    e.run_worker()
    s = e.summary()
    new = e.plans()["L002"]
    refused = [a["detail"]["reason"] for a in audit_events(e, "approval_refused") if a["lead_id"] == "L002"]
    m = {"old_hash": old["plan_hash"][:12], "new_hash": new["plan_hash"][:12], "new_rule": new["rule"],
         "l002_writes": sum(1 for x in s["effects"] if x["lead_id"] == "L002"), "refusals": refused,
         "replan_events": len(audit_events(e, "lead_changed_replan"))}
    ok = old["plan_hash"] != new["plan_hash"] and m["l002_writes"] == 0 and "hash_mismatch" in refused and \
        m["replan_events"] == 1
    return ok, m, None


def f21():
    e = new_env()
    e.run_worker()
    e.approve_all()
    e.faults(error_next=10_000, fail_paths=["/crm/leads/", "/crm/tasks", "/slack"])
    p = e.spawn_worker(SLEEP_SCALE="1", BACKOFF_BASE_S="4", BACKOFF_CAP_S="8", POLL_INTERVAL_S="30")
    out, t0 = "", time.time()
    while time.time() - t0 < 30:
        line = p.stdout.readline()
        out += line
        if '"http_retry"' in line:
            break
    t_sig = time.time()
    p.send_signal(signal.SIGTERM)
    rest, _ = p.communicate(timeout=30)
    elapsed = round(time.time() - t_sig, 2)
    e.logs.append(out + rest)
    st = e.store()
    clean = (st.get_meta("run_state") or {}).get("state")
    st.close()
    m = {"exit": p.returncode, "seconds_to_exit": elapsed, "run_state": clean}
    return p.returncode == 0 and elapsed < 5 and clean == "stopped_clean", m, None


def f22():
    leaks = sum(e.secret_leaks() for e in ALL_ENVS)
    return leaks == 0, {"environments_checked": len(ALL_ENVS), "leaks": leaks}, None


FUNCS = {f"F{i:02d}": globals()[f"f{i:02d}"] for i in range(1, 23)}


def run_all() -> list[dict]:
    results = []
    for sc in SCEN["scenarios"]:
        t0 = time.time()
        try:
            ok, m, b = FUNCS[sc["id"]]()
        except Exception as ex:  # a crash in a scenario is a failure, not a suite abort
            ok, m, b = False, {"exception": f"{type(ex).__name__}: {ex}"}, None
        results.append({"id": sc["id"], "name": sc["name"], "expect": sc["expect"], "passed": bool(ok),
                        "runtime": m, "baseline": b, "seconds": round(time.time() - t0, 2)})
        print(f"{sc['id']} {'PASS' if ok else 'FAIL'}  {sc['name']}  ({results[-1]['seconds']}s)")
    for e in ALL_ENVS:
        e.close()
    ALL_ENVS.clear()
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    res = run_all()
    n = sum(r["passed"] for r in res)
    print(f"failure suite: {n}/{len(res)} passed")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))
    return 0 if n == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
