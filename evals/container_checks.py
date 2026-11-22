"""Container static checks as an eval: this repo vs the naive fixtures, plus a build-context canary test.

The canary test copies the repo to a temp dir, plants a random canary in
secrets/crm_token.txt, .env, state/state.db and outputs/tmp/, then computes the
exact file list Docker would send to the builder after .dockerignore. Any
context file holding the canary could end up in a layer via a COPY. It also
checks that every COPY source in the Dockerfile exists in the filtered context.
"""
from __future__ import annotations

import secrets
import shutil
import tempfile
from pathlib import Path

from tools.container_lint import build_context, lint_compose, lint_dockerfile, lint_dockerignore, parse_dockerfile

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "evals" / "fixtures"


def count(findings):
    return {"errors": sum(f["severity"] == "error" for f in findings),
            "warnings": sum(f["severity"] == "warning" for f in findings),
            "ids": sorted({f["id"] for f in findings if f["severity"] == "error"})}


def canary_context(ignore_text: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ctx-"))
    dst = tmp / "repo"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "state"))
    canary = "canary-" + secrets.token_hex(12)
    planted = ["secrets/crm_token.txt", ".env", "state/state.db", "outputs/tmp/debug.log"]
    for rel in planted:
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(canary)
    files = build_context(dst, ignore_text)
    leaked = [f for f in files if canary.encode() in (dst / f).read_bytes()]
    shutil.rmtree(tmp, ignore_errors=True)
    return {"context_files": len(files), "planted": len(planted), "canary_files_in_context": len(leaked),
            "leaked": leaked}


def copy_sources_present() -> dict:
    files = set(build_context(ROOT, (ROOT / ".dockerignore").read_text()))
    missing = []
    for ins in parse_dockerfile((ROOT / "Dockerfile").read_text()):
        if ins["op"] == "COPY" and "--from=" not in ins["args"]:
            for src in [a for a in ins["args"].split() if not a.startswith("--")][:-1]:
                s = src.rstrip("/")
                if not any(f == s or f.startswith(s + "/") for f in files):
                    missing.append(src)
    return {"missing_copy_sources": missing}


def run() -> dict:
    repo = (lint_dockerfile((ROOT / "Dockerfile").read_text()) + lint_dockerignore((ROOT / ".dockerignore").read_text())
            + lint_compose((ROOT / "compose.yaml").read_text()))
    naive = (lint_dockerfile((FIX / "Dockerfile.naive").read_text()) + lint_dockerignore((FIX / "dockerignore.naive").read_text())
             + lint_compose((FIX / "compose.naive.yaml").read_text()))
    return {"lint_repo": count(repo), "lint_naive": count(naive),
            "canary_repo": canary_context((ROOT / ".dockerignore").read_text()),
            "canary_naive": canary_context((FIX / "dockerignore.naive").read_text()),
            **copy_sources_present()}
