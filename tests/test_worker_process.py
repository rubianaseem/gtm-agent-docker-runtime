"""Process-level tests: real subprocesses and real signals (the container lifecycle without Docker)."""
import signal
import time
import unittest

from evals.harness import Env


class WorkerProcessTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_plan_then_approve_then_execute_once(self):
        r = self.env.run_worker()
        self.assertEqual(r.returncode, 0, r.stdout[-500:])
        self.assertEqual(self.env.summary()["writes"], 0)  # nothing without approval
        approved = self.env.approve_all()
        self.env.run_worker()
        s = self.env.summary()
        self.assertEqual(s["writes"], sum(len(self.env.plans()[l]["ops"]) for l in approved))
        self.assertEqual(s["duplicate_effects"], 0)
        q = self.env.cli("verify-audit")
        self.assertEqual(q.returncode, 0)

    def test_kill_after_commit_then_resume(self):
        self.env.run_worker()
        self.env.approve_all()
        r = self.env.run_worker(FAULT_KILL_AT="L004:op1_sent")
        self.assertEqual(r.returncode, -signal.SIGKILL)
        self.env.run_worker()
        self.assertEqual(self.env.summary()["duplicate_effects"], 0)

    def test_sigterm_clean_exit(self):
        p = self.env.spawn_worker(POLL_INTERVAL_S="30", SLEEP_SCALE="1")
        t0 = time.time()
        while time.time() - t0 < 20:
            if '"worker_started"' in "".join(self.env.logs) or (self.env.state / "heartbeat.json").exists():
                break
            time.sleep(0.05)
        time.sleep(0.3)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0, out[-500:])
        self.assertIn('"shutdown_requested"', out)
        st = self.env.store()
        self.assertEqual(st.get_meta("run_state")["state"], "stopped_clean")
        st.close()


if __name__ == "__main__":
    unittest.main()


class ReviewQueueCsvTest(unittest.TestCase):
    def test_csv_lists_every_planned_lead(self):
        env = Env()
        try:
            env.run_worker()
            rows = (env.state / "review_queue.csv").read_text().strip().splitlines()
            self.assertEqual(len(rows) - 1, len(env.plans()))
            self.assertTrue(rows[0].startswith("lead_id,status,decision"))
        finally:
            env.close()
