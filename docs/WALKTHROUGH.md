# Walk through it line by line

This is the order I would read the code in if I were learning containers for GTM agents. Each section quotes a few lines and says what they do and why.

## 1. Dockerfile: two stages, pinned, non-root

```dockerfile
FROM python:3.12.12-slim-bookworm@sha256:593bd06e... AS builder
```
`AS builder` names the first stage. The tag says which Python; the `@sha256:` digest says exactly which image bytes. Tags can be moved by the publisher; a digest cannot, so every rebuild starts from the same base.

```dockerfile
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt
```
Dependencies go into a venv in the builder. Copying `requirements.txt` on its own first means Docker can cache this layer until the requirements change.

```dockerfile
RUN /opt/venv/bin/python -m compileall -q /app/runtime
```
Byte-compiles now, because at runtime the root filesystem is read-only and Python could not write `__pycache__`.

```dockerfile
FROM python:3.12.12-slim-bookworm@sha256:593bd06e... AS runtime
RUN groupadd --system --gid 10001 agent && useradd --system --uid 10001 ...
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
```
A fresh stage. Only the venv and the app come across. Nothing else from the builder (pip caches, build tools) ends up in the final image.

```dockerfile
USER 10001:10001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "runtime", "healthcheck"]
ENTRYPOINT ["python", "-m", "runtime"]
CMD ["run"]
```
- `USER` with a numeric id: the process cannot write outside `/data` and is not root if something escapes.
- Exec form (the JSON array) makes Python PID 1, so `docker stop` delivers SIGTERM to my code. Shell form would wrap it in `/bin/sh -c`, which does not pass signals on.
- The healthcheck is the same Python, so the image needs no curl.
- `ENTRYPOINT` + `CMD` means `docker compose run --rm worker approve ...` reuses the image for the CLI.

There is no `ARG` or `ENV` holding a secret. `tests/test_container_static.py` fails if anyone adds one.

## 2. .dockerignore: what never reaches the builder

```
.env
.env.*
secrets/
state/
```
The build context is every file Docker sends to the builder. If `secrets/crm_token.txt` were in it, one careless `COPY . .` would bake it into a layer. `evals/container_checks.py` plants a canary in four such files and checks that none of them reach the computed context.

## 3. compose.yaml: the worker block

```yaml
    read_only: true
    tmpfs:
      - "/tmp:size=16m"
    user: "10001:10001"
    cap_drop:
      - ALL
    security_opt:
      - "no-new-privileges:true"
    secrets:
      - crm_token
      - anthropic_api_key
    volumes:
      - "agent-state:/data"
    stop_grace_period: 30s
```
- `read_only` plus a small `tmpfs` leaves `/data` as the only place the agent can persist anything, and that is where checkpoints and the audit live.
- `secrets` mounts files at `/run/secrets/<name>`. They are not environment variables, so they do not show up in `docker inspect`.
- `stop_grace_period: 30s` is how long Docker waits after SIGTERM before SIGKILL. The worker needs well under a second (measured 0.01 s in F21).
- `depends_on: condition: service_healthy` makes the worker wait for the mock's healthcheck.
- The network is `internal: true`, so the default stack has no internet access.

## 4. runtime/config.py: reading a secret

```python
file_var = env.get(f"{name}_FILE")
...
p = secrets_dir / name.lower()
...
if value is None and env.get(name):
    value, source = env[name].strip(), "env"
    jsonlog.log("warning", "secret_from_env", ...)
...
jsonlog.register_secret(value)
```
File first (the `_FILE` convention many official images use), then the compose mount, then a plain env var with a warning. Every loaded value is registered with the log redactor before it can be logged anywhere.

## 5. runtime/worker.py: signals and the stop flag

```python
signal.signal(signal.SIGTERM, self._on_signal)
...
def _on_signal(self, signum, _frame):
    self.stop.set()
```
The handler only sets a `threading.Event`. The work loop checks `self.stop.is_set()` between steps and between ops, so it never stops halfway through writing state. Backoff sleeps use `self.stop_event.wait(seconds)` instead of `time.sleep`, so a SIGTERM during a 20-second backoff wakes the worker straight away.

```python
def finish(self, reason):
    self.store.set_meta("run_state", {"state": "stopped_clean", ...})
```
On the way out it writes a clean-stop marker. At start, `run_state == "running"` means the last run did not exit cleanly, and the audit records `recovered_after_unclean_stop`.

## 6. runtime/state.py: one transaction per step

```python
self.conn.execute("BEGIN IMMEDIATE")
... INSERT ... ON CONFLICT(lead_id) DO UPDATE ...
self.conn.execute("COMMIT")
```
Each step's result and the new step name are committed together. After a kill, the lead is either at the old step (redo it; steps are pure) or the new one. Round robin hashes the lead id instead of using a counter, so a redone step picks the same rep.

## 7. runtime/worker.py: executing an approved plan

```python
key = sha256_hex(f"{plan['plan_hash']}:{i}")[:40]
prior = self.store.get_op(key)
if prior and prior["status"] == "done":
    continue
self.store.put_op(key, ..., "inflight", attempts)
resp = self._send_op(op, key, plan["crm_version"] if op["op"] == "update_lead" else None)
...
self.store.put_op(key, ..., "done", attempts, resp)
```
The key comes from the plan and the op position, so a restart computes the same key. The ledger says `inflight` before the call and `done` after. If the process dies in between, the next run sends the same key again and the server returns its stored answer instead of writing twice (scenario F03). `If-Match` carries the record version the human approved; a 412 means someone changed the lead, so nothing is written and the lead is re-planned (F09).

## 8. runtime/approval.py: the gate

```python
current = plan_hash(plan)
if current != plan.get("plan_hash"):
    return False, "plan_edited_after_hashing", rec
if rec.get("plan_hash") != current:
    return False, "hash_mismatch", rec
```
The executor recomputes the hash from the stored plan every time. Editing the review file does nothing (F10). Editing the stored plan breaks the first check. A fresh plan after a change breaks the second.

## 9. runtime/http_client.py: retries

```python
if e.code not in RETRYABLE:
    raise HttpError(e.code, last_body, attempt)
if e.code == 429:
    ra = e.headers.get("Retry-After")
```
401, 403, 404 and 412 are never retried; retrying a wrong token only adds load (the naive baseline sent 5 requests in F15). 429 uses the server's Retry-After. Everything else uses capped exponential backoff with jitter derived from a hash, so test runs are reproducible.

## 10. runtime/healthcheck.py

```python
if hb.get("status") in ("fatal", "stopped"):
    return False, ...
if age > max_age_s:
    return False, f"heartbeat stale ..."
con = sqlite3.connect(f"file:{state_dir / 'state.db'}?mode=ro", uri=True, timeout=2)
```
Healthy means the loop is alive (fresh heartbeat) and the state is readable. It opens SQLite read-only so the probe can never change state.

## Try it

```bash
python run_demo.py
FAULT_KILL_AT=L004:op0_sent python -m runtime run --once   # with the mock running; see SETUP.md
python -m evals.failure_suite
```
