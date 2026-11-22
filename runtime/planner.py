"""Turn a routing decision into a reviewable plan with a SHA-256 hash.

The hash covers every op, the CRM record version the plan was made against,
and the exact text of the task and Slack draft. Any edit gives a new hash and
voids an earlier approval.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .state import sha256_hex

PLAN_SCHEMA = "plan.v1"


def due_date(received_at: str, business_days: int) -> str:
    try:
        d = datetime.strptime(received_at[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return "unknown"
    added = 0
    while added < business_days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.strftime("%Y-%m-%d")


def build_plan(lead: dict, crm_version: int, routed: dict, scored: dict, summary: str, summary_source: str,
               screen_flags: list, rules: dict) -> dict:
    ops = []
    decision = routed["decision"]
    if decision in ("assign", "keep_owner"):
        if decision == "assign":
            ops.append({"op": "update_lead", "lead_id": lead["lead_id"],
                        "set": {"owner_id": routed["owner_id"], "lead_status": "Assigned"}})
        ops.append({"op": "create_task", "lead_id": lead["lead_id"], "owner_id": routed["owner_id"],
                    "subject": f"Follow up inbound lead {lead['lead_id']}",
                    "body": summary, "due": due_date(lead.get("received_at", ""), rules["task_due_business_days"])})
        ops.append({"op": "slack_notify", "lead_id": lead["lead_id"], "channel": rules["slack_channel"],
                    "text": f"[mock] {lead['lead_id']} -> {routed['owner_id']} ({routed['rule']}, score {scored['score']}). {summary}"})
    elif decision == "disqualify":
        ops.append({"op": "update_lead", "lead_id": lead["lead_id"], "set": {"lead_status": "Unqualified"}})
    plan = {
        "schema": PLAN_SCHEMA,
        "lead_id": lead["lead_id"],
        "crm_version": crm_version,
        "rules_version": rules["version"],
        "decision": decision,
        "rule": routed["rule"],
        "owner_id": routed["owner_id"],
        "score": scored["score"],
        "score_reasons": scored["reasons"],
        "blocking": routed["blocking"],
        "warnings": sorted(set(routed["warnings"] + [f"quarantined:{f}" for f in screen_flags])),
        "summary_source": summary_source,
        "ops": ops,
        "executable": bool(ops) and not routed["blocking"],
    }
    plan["plan_hash"] = plan_hash(plan)
    return plan


def plan_hash(plan: dict) -> str:
    return sha256_hex({k: v for k, v in plan.items() if k != "plan_hash"})
