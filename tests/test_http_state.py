import json
import tempfile
import unittest
from pathlib import Path

from runtime import jsonlog, mock_services
from runtime.config import Settings
from runtime.http_client import CrmClient, HttpError, backoff_delay
from runtime.state import StateStore, verify_audit


class BackoffTest(unittest.TestCase):
    def test_deterministic_and_capped(self):
        self.assertEqual(backoff_delay(3, 0.5, 20, "k"), backoff_delay(3, 0.5, 20, "k"))
        for a in range(1, 12):
            d = backoff_delay(a, 0.5, 4, "k")
            self.assertLessEqual(d, 4)
            self.assertGreaterEqual(d, min(4, 0.5 * 2 ** (a - 1)) * 0.5)


class ClientAgainstMockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        jsonlog.MIN_LEVEL = 50
        cls.srv, cls.state = mock_services.start_in_thread("unit-token-123456")
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        jsonlog.MIN_LEVEL = 20

    def client(self, token="unit-token-123456"):
        s = Settings()
        s.sleep_scale = 0.001
        return CrmClient(self.url, token, s)

    def test_retry_after_honoured(self):
        with self.state.lock:
            self.state.faults.update(rate_limit_next=2, retry_after=3)
        c = self.client()
        c.request("GET", "/crm/leads")
        self.assertEqual(c.stats["rate_limited"], 2)
        self.assertAlmostEqual(c.stats["slept_s"], 2 * 3 * 0.001, places=4)

    def test_401_not_retried(self):
        c = self.client("wrong")
        with self.assertRaises(HttpError) as cm:
            c.request("GET", "/crm/leads")
        self.assertEqual((cm.exception.status, cm.exception.attempts), (401, 1))

    def test_idempotent_replay_and_if_match(self):
        c = self.client()
        v = c.request("GET", "/crm/leads/L002")["version"]
        r1 = c.request("PATCH", "/crm/leads/L002", {"set": {"lead_status": "Working"}}, idem_key="k-1", if_match=v)
        r2 = c.request("PATCH", "/crm/leads/L002", {"set": {"lead_status": "Working"}}, idem_key="k-1", if_match=v)
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(r1["version"], r2["version"])
        with self.assertRaises(HttpError) as cm:
            c.request("PATCH", "/crm/leads/L002", {"set": {"lead_status": "New"}}, idem_key="k-2", if_match=v)
        self.assertEqual(cm.exception.status, 412)

    def test_lost_response_retried_without_duplicate(self):
        c = self.client()
        before = len(self.state.effects)
        with self.state.lock:
            self.state.faults.update(drop_after_commit_next=1)
        c.request("POST", "/crm/tasks", {"lead_id": "L009", "owner_id": "x", "subject": "s", "body": "b", "due": "d"},
                  idem_key="task-unit-1")
        self.assertEqual(len(self.state.effects) - before, 1)
        self.assertGreaterEqual(c.stats["conn_errors"], 1)


class StateAuditTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.st = StateStore(self.dir)

    def tearDown(self):
        self.st.close()

    def test_chain_detects_edit(self):
        for i in range(3):
            self.st.audit("e", "test", f"L{i}", n=i)
        self.assertTrue(verify_audit(self.st.audit_path)[0])
        lines = self.st.audit_path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["detail"]["n"] = 99
        lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        self.st.audit_path.write_text("\n".join(lines) + "\n")
        ok, _, msg = verify_audit(self.st.audit_path)
        self.assertFalse(ok)
        self.assertIn("line 2", msg)

    def test_torn_tail_moved_aside(self):
        self.st.audit("e", "test")
        with self.st.audit_path.open("a") as f:
            f.write('{"half": ')
        torn = self.st.repair_audit_tail()
        self.assertIsNotNone(torn)
        self.assertTrue((self.dir / "audit.torn.jsonl").exists())
        self.assertTrue(verify_audit(self.st.audit_path)[0])
        self.assertIsNone(self.st.repair_audit_tail())

    def test_lead_round_trip(self):
        self.st.save_lead("L1", 2, "h", "fetched", "in_progress", {"raw": {"a": 1}})
        self.assertEqual(self.st.get_lead("L1")["data"], {"raw": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
