"""Durable state on the mounted volume: SQLite checkpoints plus a hash-chained audit log.

The container root filesystem is read-only; only STATE_DIR (a named volume in
compose, ./state in a venv) is writable. Every step result is committed in one
transaction, so a SIGKILL between steps loses at most the step that was running,
and re-running that step is safe because steps are pure functions of saved input.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

STEPS = ["fetched", "screened", "enriched", "scored", "routed", "planned"]
TERMINAL = {"executed", "closed_no_action"}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(obj) -> str:
    data = obj if isinstance(obj, str) else canonical(obj)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in ("review_queue", "approvals"):
            (self.dir / sub).mkdir(exist_ok=True)
        self.db_path = self.dir / "state.db"
        self.audit_path = self.dir / "audit.jsonl"
        self.conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._init()

    def _init(self):
        c = self.conn
        c.execute("""CREATE TABLE IF NOT EXISTS leads(
            lead_id TEXT PRIMARY KEY, crm_version INTEGER, content_hash TEXT,
            step TEXT, status TEXT, data TEXT, plan_hash TEXT, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ops(
            idem_key TEXT PRIMARY KEY, lead_id TEXT, plan_hash TEXT, op_index INTEGER,
            op_type TEXT, status TEXT, attempts INTEGER, response TEXT, updated_at TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ---- leads -------------------------------------------------------------
    def get_lead(self, lead_id: str) -> dict | None:
        row = self.conn.execute("SELECT lead_id, crm_version, content_hash, step, status, data, plan_hash "
                                "FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
        if not row:
            return None
        return {"lead_id": row[0], "crm_version": row[1], "content_hash": row[2], "step": row[3],
                "status": row[4], "data": json.loads(row[5] or "{}"), "plan_hash": row[6]}

    def all_leads(self) -> list[dict]:
        ids = [r[0] for r in self.conn.execute("SELECT lead_id FROM leads ORDER BY lead_id")]
        return [self.get_lead(i) for i in ids]

    def save_lead(self, lead_id, crm_version, content_hash, step, status, data, plan_hash=None):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO leads(lead_id, crm_version, content_hash, step, status, data, plan_hash, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(lead_id) DO UPDATE SET crm_version=excluded.crm_version, "
                "content_hash=excluded.content_hash, step=excluded.step, status=excluded.status, "
                "data=excluded.data, plan_hash=excluded.plan_hash, updated_at=excluded.updated_at",
                (lead_id, crm_version, content_hash, step, status, json.dumps(data, sort_keys=True),
                 plan_hash, now_iso()))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---- ops ledger (idempotent execution) -----------------------------------
    def get_op(self, idem_key: str) -> dict | None:
        row = self.conn.execute("SELECT idem_key, lead_id, plan_hash, op_index, op_type, status, attempts, response "
                                "FROM ops WHERE idem_key=?", (idem_key,)).fetchone()
        if not row:
            return None
        keys = ["idem_key", "lead_id", "plan_hash", "op_index", "op_type", "status", "attempts", "response"]
        return dict(zip(keys, row))

    def put_op(self, idem_key, lead_id, plan_hash, op_index, op_type, status, attempts=0, response=None):
        self.conn.execute(
            "INSERT INTO ops(idem_key, lead_id, plan_hash, op_index, op_type, status, attempts, response, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(idem_key) DO UPDATE SET status=excluded.status, "
            "attempts=excluded.attempts, response=excluded.response, updated_at=excluded.updated_at",
            (idem_key, lead_id, plan_hash, op_index, op_type, status, attempts,
             json.dumps(response) if response is not None else None, now_iso()))

    def ops_for(self, lead_id: str) -> list[dict]:
        keys = [r[0] for r in self.conn.execute("SELECT idem_key FROM ops WHERE lead_id=? ORDER BY op_index", (lead_id,))]
        return [self.get_op(k) for k in keys]

    # ---- meta ----------------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---- audit -----------------------------------------------------------------
    def _last_audit_hash(self) -> str:
        if not self.audit_path.exists():
            return "0" * 64
        last = "0" * 64
        with self.audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line)["hash"]
                    except (ValueError, KeyError):
                        break  # torn final line after a crash; verify_audit reports it
        return last

    def repair_audit_tail(self) -> str | None:
        """After a crash mid-append the last line can be torn. Move it aside (never drop it)
        to audit.torn.jsonl so the chain stays verifiable. Returns the torn text if any."""
        if not self.audit_path.exists():
            return None
        raw = self.audit_path.read_bytes()
        if not raw or raw.endswith(b"\n") and self._line_ok(raw.rstrip(b"\n").rsplit(b"\n", 1)[-1]):
            return None
        body, _, tail = raw.rstrip(b"\n").rpartition(b"\n")
        if self._line_ok(tail):
            # complete record that only lacks its newline
            self.audit_path.write_bytes(raw.rstrip(b"\n") + b"\n")
            return None
        with (self.dir / "audit.torn.jsonl").open("ab") as f:
            f.write(tail + b"\n")
        tmp = self.audit_path.with_suffix(".tmp")
        tmp.write_bytes(body + b"\n" if body else b"")
        os.replace(tmp, self.audit_path)
        return tail.decode("utf-8", "replace")

    @staticmethod
    def _line_ok(line: bytes) -> bool:
        try:
            rec = json.loads(line.decode("utf-8"))
            return isinstance(rec, dict) and "hash" in rec
        except (ValueError, UnicodeDecodeError):
            return False

    def audit(self, event: str, actor: str, lead_id: str | None = None, **detail) -> dict:
        rec = {"ts": now_iso(), "event": event, "actor": actor, "lead_id": lead_id, "detail": detail,
               "prev": self._last_audit_hash()}
        rec["hash"] = sha256_hex({k: v for k, v in rec.items()})
        line = canonical(rec) + "\n"
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return rec

    def audit_events(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        out = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"event": "__torn__", "raw": line[:80]})
        return out


def verify_audit(path: Path) -> tuple[bool, int, str]:
    """Recompute the chain. Returns (ok, records, message)."""
    path = Path(path)
    if not path.exists():
        return True, 0, "no audit log yet"
    prev = "0" * 64
    n = 0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return False, n, f"line {i}: not JSON (torn or edited)"
        h = rec.pop("hash", None)
        if rec.get("prev") != prev:
            return False, n, f"line {i}: prev hash does not match line {i - 1}"
        if sha256_hex(rec) != h:
            return False, n, f"line {i}: content hash mismatch (edited)"
        prev = h
        n += 1
    return True, n, f"{n} records, chain intact"
