"""Structured JSON logs, one object per line on stdout.

Any value registered as a secret is replaced with [REDACTED] in every line, and
email addresses are masked, so `docker logs` never carries a token or a
prospect's address.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time

_SECRETS: set[str] = set()
_LOCK = threading.Lock()
_STREAM = sys.stdout
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{6,}")
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
MIN_LEVEL = 20


def register_secret(value: str) -> None:
    if value and len(value) >= 4:
        with _LOCK:
            _SECRETS.add(value)


def clear_secrets() -> None:
    with _LOCK:
        _SECRETS.clear()


def set_stream(stream) -> None:
    global _STREAM
    _STREAM = stream


def redact(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, "[REDACTED]")
    text = BEARER_RE.sub(r"\1[REDACTED]", text)
    return EMAIL_RE.sub("[email]", text)


def log(level: str, event: str, **fields) -> None:
    if _LEVELS.get(level, 20) < MIN_LEVEL:
        return
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "event": event}
    rec.update(fields)
    line = redact(json.dumps(rec, default=str, sort_keys=False))
    with _LOCK:
        try:
            _STREAM.write(line + "\n")
            _STREAM.flush()
        except (ValueError, OSError):
            pass
