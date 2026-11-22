"""End-to-end demo without Docker: the same processes compose would start, run as local subprocesses.

  python run_demo.py

1. creates dev secrets (random token; empty Anthropic key -> template summaries)
2. starts the mock CRM + Slack service (python -m runtime mock)
3. starts the worker loop (python -m runtime run), waits for 14 plans, sends SIGTERM
4. prints the review queue
5. approves three plans BY RUNNING THE APPROVE COMMAND AS rubia.naseem. The demo does this
   so it can finish unattended; in real use you read the plan and run the command yourself.
6. restarts the worker with an injected SIGKILL straight after a committed write, restarts again,
   and shows that the write was de-duplicated, not repeated
7. prints mock-side counts, the audit chain check and the health probe
Transcript: outputs/demo_transcript.txt. State: state/demo-<timestamp>/ (git-ignored).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
LINES: list[str] = []


def say(text: str = ""):
    print(text)
    LINES.append(text)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    subprocess.run([sys.executable, "tools/make_dev_secrets.py"], cwd=ROOT, check=True, capture_output=True)
    state = ROOT / "state" / time.strftime("demo-%Y%m%d-%H%M%S")
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "FAULT_"))}
    env.update({"STATE_DIR": str(state), "CRM_BASE_URL": f"http://127.0.0.1:{port}",
                "CRM_TOKEN_FILE": str(ROOT / "secrets" / "crm_token.txt"), "POLL_INTERVAL_S": "1",
                "BACKOFF_BASE_S": "0.2", "PYTHONDONTWRITEBYTECODE": "1"})
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY_FILE"):
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE", "ANTHROPIC_MODEL"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    py = sys.executable
    mock = subprocess.Popen([py, "-m", "runtime", "mock", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        say("## 1. Worker starts, plans every lead, then gets SIGTERM (like docker stop)")
        w = subprocess.Popen([py, "-m", "runtime", "run"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        t0 = time.time()
        while time.time() - t0 < 30:
            q = state / "review_queue.csv"
            if q.exists() and len(q.read_text().splitlines()) >= 15:
                break
            time.sleep(0.2)
        say(f"  plans ready after {time.time() - t0:.1f}s")
        hc = subprocess.run([py, "-m", "runtime", "healthcheck"], cwd=ROOT, env=env, capture_output=True, text=True)
        say(f"  healthcheck while running: exit {hc.returncode}, {hc.stdout.strip()}")
        t1 = time.time()
        w.send_signal(signal.SIGTERM)
        out, _ = w.communicate(timeout=30)
        say(f"  SIGTERM to exit: {time.time() - t1:.2f}s")
        for line in out.splitlines():
            ev = json.loads(line)
            if ev["event"] in ("secret_loaded", "worker_started", "untrusted_text_quarantined", "shutdown_requested",
                               "worker_stopped"):
                say("  " + json.dumps({k: ev[k] for k in ev if k != "ts"}))
        say(f"  exit code {w.returncode}")
        say()
        say("## 2. Review queue (python -m runtime queue)")
        q = subprocess.run([py, "-m", "runtime", "queue"], cwd=ROOT, env=env, capture_output=True, text=True)
        for line in q.stdout.splitlines():
            say("  " + line)
        say()
        say("## 3. Approvals bound to the full plan hash (demo runs these as rubia.naseem)")
        attempts = [("L001", "rubia.naseem", True), ("L004", "rubia.naseem", True), ("L007", "rubia.naseem", True),
                    ("L002", "agent:worker", True), ("L014", "rubia.naseem", False)]
        for lid, who, full in attempts:
            plan = json.loads((state / "review_queue" / f"{lid}.json").read_text())
            h = plan["plan_hash"] if full else plan["plan_hash"][:12]
            r = subprocess.run([py, "-m", "runtime", "approve", lid, "--hash", h, "--approver", who], cwd=ROOT,
                               env=env, capture_output=True, text=True)
            say(f"  approve {lid} as {who} with {'full hash' if full else '12-char prefix'}: {r.stdout.strip()}")
        say()
        say("## 4. Execute, with a SIGKILL injected right after L004's first write commits")
        r = subprocess.run([py, "-m", "runtime", "run", "--once"], cwd=ROOT, env=dict(env, FAULT_KILL_AT="L004:op0_sent"),
                           capture_output=True, text=True)
        say(f"  first run exit code {r.returncode} (SIGKILL)")
        r = subprocess.run([py, "-m", "runtime", "run", "--once"], cwd=ROOT, env=env, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            ev = json.loads(line)
            if ev["event"] in ("recovered_after_unclean_stop", "op_resend_same_key", "worker_stopped"):
                say("  " + json.dumps({k: ev[k] for k in ev if k not in ("ts",)}))
        say(f"  second run exit code {r.returncode}")
        say()
        say("## 5. What the mock CRM and mock Slack received")
        summary = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/_admin/summary", timeout=3).read())
        say(f"  writes {summary['writes']}, duplicate effects {summary['duplicate_effects']}, tasks "
            f"{summary['tasks']}, mock Slack messages {summary['slack_messages']}")
        for m in summary["slack"]:
            say(f"  slack(mock) {m['channel']}: {m['text']}")
        st = subprocess.run([py, "-m", "runtime", "status"], cwd=ROOT, env=env, capture_output=True, text=True)
        say("  lead status counts: " + json.dumps(json.loads(st.stdout), sort_keys=True))
        va = subprocess.run([py, "-m", "runtime", "verify-audit"], cwd=ROOT, env=env, capture_output=True, text=True)
        say(f"  audit chain: {va.stdout.strip()}")
        hc = subprocess.run([py, "-m", "runtime", "healthcheck"], cwd=ROOT, env=env, capture_output=True, text=True)
        say(f"  healthcheck after the worker exited: exit {hc.returncode}, {hc.stdout.strip()}")
    finally:
        mock.send_signal(signal.SIGTERM)
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
    OUT.mkdir(exist_ok=True)
    (OUT / "demo_transcript.txt").write_text("\n".join(LINES) + "\n")
    sample = OUT / "sample_run"
    sample.mkdir(exist_ok=True)
    shutil.copy(state / "review_queue.csv", sample / "review_queue.csv")
    for lid in ("L001", "L007", "L009"):
        shutil.copy(state / "review_queue" / f"{lid}.json", sample / f"plan_{lid}.json")
    shutil.copy(state / "audit.jsonl", sample / "audit.jsonl")
    print(f"\ntranscript written to {(OUT / 'demo_transcript.txt').relative_to(ROOT)}; state in {state.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
