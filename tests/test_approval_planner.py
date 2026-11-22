import json
import tempfile
import time
import unittest
from pathlib import Path

from runtime import approval, planner
from runtime.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
RULES = {"version": "t", "slack_channel": "#x", "task_due_business_days": 1}


def make_plan(text="hello"):
    lead = {"lead_id": "L1", "received_at": "2026-11-20T09:00:00Z"}
    routed = {"decision": "assign", "owner_id": "rep.uk.mm.1", "rule": "R05", "blocking": [], "warnings": []}
    return planner.build_plan(lead, 3, routed, {"score": 50, "reasons": []}, text, "template", [], RULES)


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        StateStore(self.state).close()
        self.plan = make_plan()
        (self.state / "review_queue" / "L1.json").write_text(json.dumps(self.plan))

    def approve(self, h=None, who="rubia.naseem", now=None):
        return approval.write_approval(self.state, ROOT / "config", "L1", h or self.plan["plan_hash"], who, 24, now=now)

    def test_valid_approval(self):
        self.approve()
        ok, reason, _ = approval.validate(self.state, ROOT / "config", self.plan)
        self.assertTrue(ok, reason)

    def test_refusals_at_write_time(self):
        for kwargs, msg in [({"who": "agent:worker"}, "agent"), ({"who": "bot:x"}, "agent"),
                            ({"who": "someone.else"}, "not on the approver list"),
                            ({"h": self.plan["plan_hash"][:12]}, "full 64"), ({"h": "0" * 64}, "does not match")]:
            with self.assertRaises(approval.ApprovalError) as cm:
                self.approve(**kwargs)
            self.assertIn(msg, str(cm.exception))

    def test_expired(self):
        self.approve(now=time.time() - 25 * 3600)
        self.assertEqual(approval.validate(self.state, ROOT / "config", self.plan)[1], "approval_expired")

    def test_edit_after_approval_voids_it(self):
        self.approve()
        edited = make_plan("hello, now edited")
        self.assertNotEqual(edited["plan_hash"], self.plan["plan_hash"])
        self.assertEqual(approval.validate(self.state, ROOT / "config", edited)[1], "hash_mismatch")
        tampered = dict(self.plan, owner_id="rep.uk.ent.1")  # hash field left unchanged
        self.assertEqual(approval.validate(self.state, ROOT / "config", tampered)[1], "plan_edited_after_hashing")

    def test_non_executable_plan_cannot_be_approved(self):
        lead = {"lead_id": "L1", "received_at": "2026-11-20T09:00:00Z"}
        routed = {"decision": "review", "owner_id": None, "rule": "R09", "blocking": ["unknown_territory:x"], "warnings": []}
        p = planner.build_plan(lead, 1, routed, {"score": 0, "reasons": []}, "t", "template", [], RULES)
        (self.state / "review_queue" / "L1.json").write_text(json.dumps(p))
        with self.assertRaises(approval.ApprovalError):
            approval.write_approval(self.state, ROOT / "config", "L1", p["plan_hash"], "rubia.naseem", 24)


class PlannerTest(unittest.TestCase):
    def test_due_date_skips_weekend(self):
        self.assertEqual(planner.due_date("2026-11-20T09:00:00Z", 1), "2026-11-23")  # Friday -> Monday
        self.assertEqual(planner.due_date("garbage", 1), "unknown")

    def test_hash_is_deterministic_and_covers_text(self):
        self.assertEqual(make_plan()["plan_hash"], make_plan()["plan_hash"])
        self.assertNotEqual(make_plan("a")["plan_hash"], make_plan("b")["plan_hash"])


if __name__ == "__main__":
    unittest.main()
