"""Static checks for Dockerfile, .dockerignore and compose.yaml (runs without Docker).

  python tools/container_lint.py                 # lint this repo, exit 1 on any error
  python tools/container_lint.py --json          # machine-readable findings
  python tools/container_lint.py --dockerfile evals/fixtures/Dockerfile.naive \
      --compose evals/fixtures/compose.naive.yaml --dockerignore evals/fixtures/dockerignore.naive

These checks are a floor, not a substitute for building the image and scanning
it (tools/image_secret_scan.py does that where Docker is available).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import miniyaml  # noqa: E402

SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIAL)", re.I)
SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
MUST_IGNORE = [".env", "secrets/", "state/", ".git"]


# ------------------------------------------------------------------ Dockerfile
def parse_dockerfile(text: str) -> list[dict]:
    instrs, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.split(None, 1)
        instrs.append({"op": parts[0].upper(), "args": parts[1] if len(parts) > 1 else ""})
        buf = ""
    stage = -1
    for ins in instrs:
        if ins["op"] == "FROM":
            stage += 1
        ins["stage"] = stage
    return instrs


def _is_exec_form(args: str) -> bool:
    try:
        v = json.loads(args)
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    except ValueError:
        return False


def lint_dockerfile(text: str) -> list[dict]:
    f = []
    ins = parse_dockerfile(text)
    froms = [i for i in ins if i["op"] == "FROM"]
    final = froms[-1]["stage"] if froms else 0
    fin = [i for i in ins if i["stage"] == final]
    for i in froms:
        image = i["args"].split()[0]
        if image.startswith("$"):
            f.append(("error", "D01", f"FROM uses a variable ({image}); pin the literal image"))
        elif not DIGEST.search(image):
            f.append(("error", "D01", f"base image not pinned to a digest: {image}"))
        if image.split("@")[0].endswith(":latest") or ":" not in image.split("@")[0]:
            f.append(("error", "D01", f"base image uses latest or no tag: {image}"))
    if len(froms) < 2 or not any(i["op"] == "COPY" and "--from=" in i["args"] for i in fin):
        f.append(("error", "D02", "not a multi-stage build that copies artefacts from a builder stage"))
    users = [i for i in fin if i["op"] == "USER"]
    if not users:
        f.append(("error", "D03", "final stage has no USER (runs as root)"))
    else:
        u = users[-1]["args"].split(":")[0].strip()
        if u in ("0", "root"):
            f.append(("error", "D03", "final stage USER is root"))
        last_user_idx = max(idx for idx, i in enumerate(fin) if i["op"] == "USER")
        later_run = [i for i in fin[last_user_idx:] if i["op"] == "RUN"]
        if later_run:
            f.append(("warning", "D03", "RUN after USER; check it does not need root"))
    hcs = [i for i in fin if i["op"] == "HEALTHCHECK"]
    if not hcs or hcs[-1]["args"].strip().upper() == "NONE":
        f.append(("error", "D04", "no HEALTHCHECK in the final stage"))
    else:
        cmd = hcs[-1]["args"].split("CMD", 1)[-1].strip()
        if not _is_exec_form(cmd):
            f.append(("warning", "D04", "HEALTHCHECK uses shell form; prefer exec form"))
    eps = [i for i in fin if i["op"] in ("ENTRYPOINT", "CMD")]
    if not eps:
        f.append(("error", "D05", "no ENTRYPOINT/CMD"))
    for i in eps:
        if not _is_exec_form(i["args"]):
            f.append(("error", "D05", f"{i['op']} in shell form: process is not PID 1 and will not get SIGTERM"))
    for i in ins:
        if i["op"] in ("ARG", "ENV"):
            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"[^\"]*\"|\S+)?", i["args"]):
                name, val = m.group(1), (m.group(2) or "").strip('"')
                if SECRET_NAME.search(name) and not name.endswith("_FILE"):
                    f.append(("error", "D06", f"{i['op']} {name} looks like a secret; ARG/ENV persist in the image"))
            if i["op"] == "ARG" and "=" not in i["args"] and SECRET_NAME.search(i["args"]):
                f.append(("error", "D06", f"ARG {i['args'].strip()} looks like a secret; use RUN --mount=type=secret"))
        if SECRET_VALUE.search(i["args"]):
            f.append(("error", "D06", f"{i['op']} contains a literal credential pattern"))
        if i["op"] in ("COPY", "ADD"):
            srcs = [a for a in i["args"].split() if not a.startswith("--")][:-1]
            for s in srcs:
                if s in (".", "./") and i["stage"] == final:
                    f.append(("error", "D07", "final stage copies the whole build context (COPY . ...)"))
                if re.search(r"(^|/)(\.env|secrets?/?|.*\.pem|id_rsa)", s):
                    f.append(("error", "D08", f"{i['op']} copies a secret-looking path: {s}"))
            if i["op"] == "ADD" and re.search(r"https?://", i["args"]):
                f.append(("error", "D09", "ADD from a remote URL (unverified download)"))
        if i["op"] == "RUN" and re.search(r"curl[^|]*\|\s*(ba)?sh", i["args"]):
            f.append(("error", "D09", "RUN pipes a download into a shell"))
    if not any(i["op"] == "STOPSIGNAL" for i in fin):
        f.append(("warning", "D10", "no STOPSIGNAL (default SIGTERM is fine if the app handles it)"))
    return [{"severity": s, "id": c, "file": "Dockerfile", "message": m} for s, c, m in f]


# ---------------------------------------------------------------- .dockerignore
def _pattern_regex(p: str) -> re.Pattern:
    p = p.strip().lstrip("/").rstrip("/")
    out, i = "", 0
    while i < len(p):
        if p[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif p[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        elif p[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(out + "$")


def load_ignore(text: str) -> list[tuple[bool, re.Pattern, str]]:
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pat = line[1:] if neg else line
        rules.append((neg, _pattern_regex(pat), line))
    return rules


def is_ignored(rel: str, rules) -> bool:
    parts = rel.split("/")
    candidates = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
    ignored = False
    for neg, rx, _ in rules:
        if any(rx.match(c) for c in candidates):
            ignored = not neg
    return ignored


def build_context(root: Path, ignore_text: str) -> list[str]:
    """Files Docker would send to the builder, after .dockerignore."""
    rules = load_ignore(ignore_text)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if d != ".git" or not is_ignored(".git", rules)]
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if not is_ignored(rel, rules):
                out.append(rel)
    return sorted(out)


def lint_dockerignore(text: str) -> list[dict]:
    rules = load_ignore(text)
    f = []
    for must in MUST_IGNORE:
        probe = must.rstrip("/") + ("/x" if must.endswith("/") else "")
        if not is_ignored(probe, rules):
            f.append(("error", "I01", f".dockerignore does not exclude {must}"))
    return [{"severity": s, "id": c, "file": ".dockerignore", "message": m} for s, c, m in f]


# -------------------------------------------------------------------- compose
def lint_compose(text: str, state_dir: str = "/data") -> list[dict]:
    f = []
    try:
        doc = miniyaml.loads(text)
    except ValueError as e:
        return [{"severity": "error", "id": "C00", "file": "compose", "message": f"unparseable: {e}"}]
    services = doc.get("services") or {}
    top_secrets = doc.get("secrets") or {}
    for name, svc in services.items():
        svc = svc or {}
        if svc.get("read_only") is not True:
            f.append(("error", "C01", f"{name}: read_only is not true"))
        if "ALL" not in (svc.get("cap_drop") or []):
            f.append(("error", "C02", f"{name}: cap_drop does not include ALL"))
        if not any("no-new-privileges" in str(x) for x in (svc.get("security_opt") or [])):
            f.append(("error", "C03", f"{name}: missing no-new-privileges"))
        user = str(svc.get("user", ""))
        if not user or user.split(":")[0] in ("0", "root"):
            f.append(("error", "C08", f"{name}: no non-root user set in compose"))
        if svc.get("privileged") is True or svc.get("network_mode") == "host":
            f.append(("error", "C09", f"{name}: privileged or host network"))
        for v in svc.get("volumes") or []:
            if "docker.sock" in str(v):
                f.append(("error", "C09", f"{name}: mounts the Docker socket"))
        env = svc.get("environment") or {}
        if isinstance(env, list):
            env = dict(str(e).split("=", 1) if "=" in str(e) else (str(e), "") for e in env)
        for k, v in env.items():
            if SECRET_NAME.search(k) and not k.endswith("_FILE"):
                f.append(("error", "C04", f"{name}: secret-like env var {k} (use compose secrets)"))
            if SECRET_VALUE.search(str(v)):
                f.append(("error", "C04", f"{name}: env {k} holds a literal credential"))
        for s in svc.get("secrets") or []:
            spec = top_secrets.get(s)
            if not spec or not (spec.get("file") or spec.get("environment") or spec.get("external")):
                f.append(("error", "C04", f"{name}: secret {s} not defined with file/environment/external"))
        if (svc.get("healthcheck") or {}).get("disable") is True:
            f.append(("error", "C06", f"{name}: healthcheck disabled"))
    worker = services.get("worker") or {}
    if not any(str(v).endswith(":" + state_dir) for v in worker.get("volumes") or []):
        f.append(("error", "C05", f"worker: no volume mounted at {state_dir} for checkpoints and audit"))
    if worker and not worker.get("secrets"):
        f.append(("error", "C04", "worker: no compose secrets used"))
    g = str(worker.get("stop_grace_period", "10s"))
    m = re.fullmatch(r"(\d+)(s|m)?", g)
    secs = int(m.group(1)) * (60 if m and m.group(2) == "m" else 1) if m else 0
    if secs < 15:
        f.append(("warning", "C07", f"worker: stop_grace_period {g} may be shorter than one backoff + checkpoint"))
    mock = services.get("mock-crm") or {}
    if mock and not mock.get("healthcheck"):
        f.append(("error", "C06", "mock-crm: no healthcheck, so depends_on cannot wait for it"))
    nets = doc.get("networks") or {}
    if not any((n or {}).get("internal") is True for n in nets.values()):
        f.append(("warning", "C10", "no internal-only network; the default stack has egress"))
    return [{"severity": s, "id": c, "file": "compose", "message": m} for s, c, m in f]


def run(dockerfile: Path, compose: Path, dockerignore: Path) -> list[dict]:
    return (lint_dockerfile(dockerfile.read_text()) + lint_dockerignore(dockerignore.read_text())
            + lint_compose(compose.read_text()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dockerfile", default=str(ROOT / "Dockerfile"))
    ap.add_argument("--compose", default=str(ROOT / "compose.yaml"))
    ap.add_argument("--dockerignore", default=str(ROOT / ".dockerignore"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    findings = run(Path(a.dockerfile), Path(a.compose), Path(a.dockerignore))
    if a.json:
        print(json.dumps(findings, indent=2))
    else:
        for x in findings:
            print(f"{x['severity'].upper():7} {x['id']} {x['file']}: {x['message']}")
        errs = sum(x["severity"] == "error" for x in findings)
        warns = sum(x["severity"] == "warning" for x in findings)
        print(f"{errs} error(s), {warns} warning(s)")
    return 1 if any(x["severity"] == "error" for x in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
