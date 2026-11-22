"""Settings and secrets.

Secrets are read at runtime, never at build time, in this order:
  1. <NAME>_FILE env var pointing at a file (Docker convention, e.g. CRM_TOKEN_FILE)
  2. /run/secrets/<name lower case> (Compose secrets mount)
  3. <NAME> env var (plain venv runs only; logged as a warning, value never logged)
Every secret value that is loaded is registered with the log redactor.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import jsonlog

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))
REPO_ROOT = Path(__file__).resolve().parent.parent


class SecretError(RuntimeError):
    """Raised when a required secret is missing. The message never holds the value."""


def read_secret(name: str, required: bool = True, env: dict | None = None) -> str | None:
    env = os.environ if env is None else env
    value = None
    source = None
    file_var = env.get(f"{name}_FILE")
    secrets_dir = Path(env.get("SECRETS_DIR", str(SECRETS_DIR)))
    if file_var:
        p = Path(file_var)
        if p.is_file():
            value, source = p.read_text(encoding="utf-8").strip(), f"file:{name}_FILE"
    if value is None:
        p = secrets_dir / name.lower()
        if p.is_file():
            value, source = p.read_text(encoding="utf-8").strip(), "compose-secret"
    if value is None and env.get(name):
        value, source = env[name].strip(), "env"
        jsonlog.log("warning", "secret_from_env", secret=name,
                    hint="prefer a file secret; env vars can leak into inspect output")
    if value == "":
        value = None
    if value is None:
        if required:
            raise SecretError(f"secret {name} is not set (looked for {name}_FILE, "
                              f"{secrets_dir}/{name.lower()}, env {name})")
        return None
    jsonlog.register_secret(value)
    jsonlog.log("info", "secret_loaded", secret=name, source=source, length=len(value))
    return value


@dataclass
class Settings:
    crm_base_url: str = "http://127.0.0.1:8088"
    state_dir: Path = Path("./state")
    config_dir: Path = REPO_ROOT / "config"
    poll_interval_s: float = 5.0
    heartbeat_max_age_s: float = 60.0
    max_attempts: int = 5
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 20.0
    retry_after_cap_s: float = 30.0
    rate_limit_per_s: float = 5.0
    sleep_scale: float = 1.0          # evals set this low so backoff runs fast
    approval_ttl_h: float = 24.0
    worker_id: str = "worker-1"
    fault_kill_at: str = ""           # "<lead_id>:<step>" for crash tests (SIGKILL self)
    fault_term_at: str = ""           # "<lead_id>:<step>" for graceful-stop tests (SIGTERM self)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls()
        s.crm_base_url = env.get("CRM_BASE_URL", s.crm_base_url).rstrip("/")
        s.state_dir = Path(env.get("STATE_DIR", str(s.state_dir)))
        s.config_dir = Path(env.get("CONFIG_DIR", str(s.config_dir)))
        for key, attr in [("POLL_INTERVAL_S", "poll_interval_s"),
                          ("HEARTBEAT_MAX_AGE_S", "heartbeat_max_age_s"),
                          ("BACKOFF_BASE_S", "backoff_base_s"),
                          ("BACKOFF_CAP_S", "backoff_cap_s"),
                          ("RATE_LIMIT_PER_S", "rate_limit_per_s"),
                          ("SLEEP_SCALE", "sleep_scale"),
                          ("APPROVAL_TTL_H", "approval_ttl_h")]:
            if env.get(key):
                setattr(s, attr, float(env[key]))
        if env.get("MAX_ATTEMPTS"):
            s.max_attempts = int(env["MAX_ATTEMPTS"])
        s.worker_id = env.get("WORKER_ID", s.worker_id)
        s.fault_kill_at = env.get("FAULT_KILL_AT", "")
        s.fault_term_at = env.get("FAULT_TERM_AT", "")
        return s
