"""Hash-bound human approval, enforced in code.

An approval is a file in STATE_DIR/approvals/<lead_id>.json written by the
`approve` command. The executor accepts it only if:
  - the approver is on config/approvers.json and is not an agent/bot/service id
  - the approval quotes the full 64-hex SHA-256 of the plan, and that hash equals
    the hash recomputed from the plan on disk right now (edits void approval)
  - it has not expired (APPROVAL_TTL_H, default 24h)
  - the plan is executable (no blocking reasons)
There is no auto-approve flag, and the worker process never writes approval files.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .planner import plan_hash
from .state import now_iso

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ApprovalError(ValueError):
    pass


def load_approvers(config_dir: Path) -> dict:
    return json.loads((Path(config_dir) / "approvers.json").read_text())


def check_identity(approver: str, cfg: dict) -> None:
    a = (approver or "").strip().lower()
    if not a:
        raise ApprovalError("approver name is required")
    if any(a.startswith(p) for p in cfg["refused_prefixes"]):
        raise ApprovalError(f"agent or service identity cannot approve: {approver}")
    if a not in cfg["approvers"]:
        raise ApprovalError(f"{approver} is not on the approver list")


def write_approval(state_dir: Path, config_dir: Path, lead_id: str, quoted_hash: str, approver: str,
                   ttl_h: float, now: float | None = None) -> dict:
    cfg = load_approvers(config_dir)
    check_identity(approver, cfg)
    if not HEX64.match(quoted_hash or ""):
        raise ApprovalError("quote the full 64-character plan hash, not a prefix")
    plan_path = Path(state_dir) / "review_queue" / f"{lead_id}.json"
    if not plan_path.exists():
        raise ApprovalError(f"no plan in the review queue for {lead_id}")
    plan = json.loads(plan_path.read_text())
    if plan_hash(plan) != quoted_hash:
        raise ApprovalError("hash does not match the current plan (it changed, or you copied the wrong one)")
    if not plan.get("executable"):
        raise ApprovalError(f"plan is not executable: {plan.get('blocking')}")
    now = time.time() if now is None else now
    rec = {"lead_id": lead_id, "plan_hash": quoted_hash, "approver": approver.strip().lower(),
           "approved_at": now_iso(now), "expires_at_epoch": now + ttl_h * 3600}
    out = Path(state_dir) / "approvals" / f"{lead_id}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    tmp.replace(out)
    return rec


def validate(state_dir: Path, config_dir: Path, plan: dict, now: float | None = None) -> tuple[bool, str, dict | None]:
    path = Path(state_dir) / "approvals" / f"{plan['lead_id']}.json"
    if not path.exists():
        return False, "no_approval", None
    try:
        rec = json.loads(path.read_text())
    except ValueError:
        return False, "approval_unreadable", None
    try:
        check_identity(rec.get("approver", ""), load_approvers(config_dir))
    except ApprovalError as e:
        return False, f"approver_refused:{e}", rec
    current = plan_hash(plan)
    if current != plan.get("plan_hash"):
        return False, "plan_edited_after_hashing", rec
    if rec.get("plan_hash") != current:
        return False, "hash_mismatch", rec
    if (time.time() if now is None else now) > float(rec.get("expires_at_epoch", 0)):
        return False, "approval_expired", rec
    if not plan.get("executable"):
        return False, "plan_not_executable", rec
    return True, "ok", rec
