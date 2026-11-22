"""Create local dev secret files for the mock stack (never committed, never in the image).

The token is random per machine. anthropic_api_key.txt is created empty so the
stack runs the template path; paste a key into it only if you want Claude.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    d = ROOT / "secrets"
    d.mkdir(exist_ok=True)
    tok = d / "crm_token.txt"
    if not tok.exists():
        tok.write_text(secrets.token_urlsafe(32) + "\n")
    key = d / "anthropic_api_key.txt"
    if not key.exists():
        key.write_text("")
    # Compose (non-swarm) bind-mounts secret files with host permissions, and the
    # container runs as uid 10001, so the files must be readable by that uid.
    # 0644 is for local dev on your own machine only.
    for p in (tok, key):
        os.chmod(p, 0o644)
    print(f"wrote {tok.relative_to(ROOT)} and {key.relative_to(ROOT)} (values not printed)")


if __name__ == "__main__":
    main()
