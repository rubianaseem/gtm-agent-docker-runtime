"""HEALTHCHECK command: exit 0 if the worker heartbeat is fresh and state is readable, else 1.

Docker runs this inside the container (exec form), so it needs no curl or shell.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def check(state_dir: Path, max_age_s: float) -> tuple[bool, str]:
    hb_path = state_dir / "heartbeat.json"
    try:
        hb = json.loads(hb_path.read_text())
    except (OSError, ValueError):
        return False, "no heartbeat"
    if hb.get("status") in ("fatal", "stopped"):
        return False, f"worker status {hb.get('status')}"
    age = time.time() - float(hb.get("ts", 0))
    if age > max_age_s:
        return False, f"heartbeat stale ({age:.0f}s > {max_age_s:.0f}s)"
    try:
        con = sqlite3.connect(f"file:{state_dir / 'state.db'}?mode=ro", uri=True, timeout=2)
        con.execute("SELECT count(*) FROM leads").fetchone()
        con.close()
    except sqlite3.Error as e:
        return False, f"state db unreadable: {type(e).__name__}"
    return True, f"ok (heartbeat {age:.0f}s, status {hb.get('status')})"


def main() -> int:
    ok, msg = check(Path(os.environ.get("STATE_DIR", "./state")), float(os.environ.get("HEARTBEAT_MAX_AGE_S", "60")))
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
