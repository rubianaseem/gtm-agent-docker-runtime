"""Build the image with canary secrets present and prove they never reach it (needs Docker).

  python tools/image_secret_scan.py

Steps:
  1. write a random canary into secrets/crm_token.txt and .env (backing up any existing files)
  2. docker build -t gtm-agent-docker-runtime:scan .
  3. scan `docker history --no-trunc`, `docker image inspect` (Env, Labels, Cmd) and
     every layer tar from `docker save` for the canary bytes
  4. restore the original files
Exit codes: 0 clean, 1 canary found, 2 Docker not available.
Not executed in the published results unless the README says so.
"""
from __future__ import annotations

import io
import secrets
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = "gtm-agent-docker-runtime:scan"


def sh(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=ROOT, capture_output=True, **kw)


def main() -> int:
    if shutil.which("docker") is None or sh("docker", "info").returncode != 0:
        print("docker not available; static checks only (python tools/container_lint.py)")
        return 2
    canary = "canary-" + secrets.token_hex(16)
    targets = [ROOT / "secrets" / "crm_token.txt", ROOT / ".env"]
    backups = {p: p.read_bytes() if p.exists() else None for p in targets}
    try:
        (ROOT / "secrets").mkdir(exist_ok=True)
        for p in targets:
            p.write_text(f"CRM_TOKEN={canary}\n" if p.name == ".env" else canary + "\n")
        b = sh("docker", "build", "--no-cache", "-t", TAG, ".")
        if b.returncode != 0:
            print(b.stderr.decode()[-2000:])
            return 1
        found = []
        hist = sh("docker", "history", "--no-trunc", TAG).stdout.decode()
        if canary in hist:
            found.append("docker history")
        insp = sh("docker", "image", "inspect", TAG).stdout.decode()
        if canary in insp:
            found.append("image config (Env/Labels/Cmd)")
        tar_bytes = sh("docker", "save", TAG).stdout
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as outer:
            for m in outer.getmembers():
                if not m.isfile():
                    continue
                data = outer.extractfile(m).read()
                if canary.encode() in data:
                    found.append(f"saved image member {m.name}")
                    continue
                try:
                    with tarfile.open(fileobj=io.BytesIO(data)) as layer:
                        for lm in layer.getmembers():
                            if lm.isfile() and canary.encode() in layer.extractfile(lm).read():
                                found.append(f"layer {m.name}: {lm.name}")
                except tarfile.TarError:
                    pass
        user = sh("docker", "image", "inspect", "-f", "{{.Config.User}}", TAG).stdout.decode().strip()
        print(f"image user: {user or '(root)'}")
        if found:
            print("CANARY FOUND in: " + "; ".join(found))
            return 1
        print("clean: canary not found in history, config or any layer")
        return 0
    finally:
        for p, data in backups.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)


if __name__ == "__main__":
    sys.exit(main())
