# Details

## Architecture

```
                 docker compose (network gtm-internal, internal: true)
 +----------------------------------------------------------------------------------+
 |  mock-crm  (same image, command: mock)          worker  (command: run, PID 1)    |
 |  +---------------------------+   HTTP + Bearer  +-------------------------------+ |
 |  | /crm/leads   GET          | <--------------- | poll -> ingest (version/hash) | |
 |  | /crm/accounts GET         |                  | screen  (untrusted boundary)  | |
 |  | /crm/leads/:id PATCH      |   Idempotency-   | enrich -> score -> route      | |
 |  |   If-Match -> 412         |   Key, If-Match  | plan (+ optional Claude line) | |
 |  | /crm/tasks POST           | <--------------- | execute approved ops only     | |
 |  | /slack/chat.postMessage   |   retries/backoff| SIGTERM -> checkpoint, exit 0 | |
 |  | faults: 429, 503, lost ack|   rate limit     +---------------+---------------+ |
 |  +---------------------------+                                  |                 |
 |          /run/secrets/crm_token (compose secret, both services) |                 |
 +-----------------------------------------------------------------|-----------------+
                                                                   v
                       volume agent-state:/data  (the only writable path besides /tmp)
                       state.db (SQLite checkpoints + op ledger)  audit.jsonl (hash chain)
                       review_queue/<lead>.json  review_queue.csv  approvals/<lead>.json
                       heartbeat.json  <- HEALTHCHECK: python -m runtime healthcheck
                                    ^
       human: python -m runtime approve <lead> --hash <64-hex> --approver <name>
```

Image: two stages from `python:3.12.12-slim-bookworm@sha256:593bd06e...`. The builder makes a venv and byte-compiles the code. The runtime stage copies only the venv, the code, the config and the sample CSVs, and runs as uid 10001. ENTRYPOINT and HEALTHCHECK use exec form. Tests, evals, docs, tools, secrets, state and `.git` are excluded by `.dockerignore`.

## Data contracts

| Contract | Where | Fields |
|---|---|---|
| Lead (input) | `GET /crm/leads` | lead_id, received_at, first_name, last_name, email, company, title, country, employees (free text), message, source, consent, owner_id, version (int) |
| Account (input) | `GET /crm/accounts?domain=` | account_id, name, domain, employees, owner_id, named_account, open_opportunity, territory |
| Plan `plan.v1` | `review_queue/<lead>.json` | lead_id, crm_version, rules_version, decision (assign, keep_owner, disqualify, review), rule, owner_id, score, score_reasons, blocking[], warnings[], summary_source, ops[], executable, plan_hash = SHA-256 of canonical JSON of every other field |
| Op | inside plan | update_lead {set: owner_id, lead_status}; create_task {owner_id, subject, body, due}; slack_notify {channel, text}. Idempotency-Key = first 40 hex of SHA-256("plan_hash:op_index") |
| Approval | `approvals/<lead>.json` | lead_id, plan_hash (full), approver, approved_at, expires_at_epoch |
| Op ledger | `state.db` table ops | idem_key, lead_id, plan_hash, op_index, op_type, status (inflight, done, stale, failed_retry_later), attempts, response |
| Audit record | `audit.jsonl` | ts, event, actor, lead_id, detail, prev, hash = SHA-256 of the record without hash |
| Heartbeat | `heartbeat.json` | ts, worker_id, pid, status (starting, running, stopped, fatal), cycle |

Steps per lead: fetched, screened, enriched, scored, routed, planned. Statuses: in_progress, pending_approval, needs_review, executing, executed, stale_needs_replan.

## Hard controls (deterministic, in code)

- No write without a valid approval: approver on the list, not agent/bot/svc/worker, full hash equal to the stored plan's recomputed hash, inside the TTL, plan executable.
- If-Match on every lead update; 412 voids the plan and re-plans.
- Free text never reaches the router; quarantined fields never reach the template, the model or the Slack text.
- Secrets only at runtime from files; registered values redacted from every log line; Bearer patterns and emails masked.
- The worker cannot widen its own scope: allowed lead fields are `owner_id` and `lead_status` (the mock rejects others).

## Evals

`python run_evals.py` runs three things and writes `evals/report.md` and `evals/results.json`.

1. Routing (`evals/cases_v1.json`, SHA-256 pinned, 40 synthetic cases). Dev cases were written while I built the rules. I wrote the 10 held-out cases after `routing-rules.v1` was frozen and did not change the rules after seeing their results. The baseline is a country-only script: exact alias lookup, `int()` on employees, first rep in the territory, defaulting to UK. Results: dev 30/30 vs 12/30; held-out 9/10 vs 5/10; review recall 11/11 vs 0/11; unsafe auto-assign 0 vs 15. Held-out miss H01 ("U.K.") is a gap in the alias table. Injection obeyed is 0 for both, because neither router reads free text. That is a design property, not a model result.
2. Container static checks (`tools/container_lint.py`, `evals/container_checks.py`): 0 errors for this repo, 29 for the naive fixtures in `evals/fixtures/`. Canary test: 0 of 4 planted secret files reach the computed build context, against 4 of 4 with a naive `.dockerignore`. Every COPY source exists in the filtered context. These are static checks only. Whether a secret lands in image layers or history is checked by `tools/image_secret_scan.py`, which needs Docker and has not been run for these results.
3. Failure-injection suite (`evals/failure_scenarios_v1.json`, 22 scenarios). The worker runs as a real subprocess with real SIGKILL and SIGTERM against the in-process mock; see the table in `evals/report.md`.

## Failure handling and recovery

| Failure | Behaviour | Proven by |
|---|---|---|
| SIGKILL / OOM between steps | resume from last committed step; same plan hash | F02 |
| SIGKILL after a write committed, before the ack | re-send with the same Idempotency-Key; server replays the stored response | F03, demo |
| SIGTERM (docker stop) | finish the current step, checkpoint, clean-stop marker, exit 0 | F04, F21 (0.01 s from inside backoff) |
| 429 with Retry-After | honour it (capped at 30 s), client-side token bucket | F05 |
| 5xx / connection reset / lost response | full-jitter exponential backoff, deterministic jitter, max 5 attempts | F06, F07 |
| Outage longer than the budget | op parked as failed_retry_later, worker keeps running, next poll resumes | F08 |
| Record changed after approval | 412, no overwrite, re-plan, old approval refused | F09 |
| Lead changed while pending | version change on ingest, re-plan, old approval refused | F20 |
| Torn audit line | tail moved to audit.torn.jsonl (kept, not dropped), chain continues | F18 |
| Missing or wrong secret | refuse to start (exit 3) / 401 not retried, no token in logs | F14, F15 |

A bug found by the suite during the build: after a SIGKILL right after the lead update committed, the next ingest saw the bumped version and treated it as an outside edit, so it re-planned instead of resuming. Fix: leads in `executing` are not re-planned on ingest; the If-Match on the re-sent update still catches a real outside edit.

## API cost assumptions

No account billing data was used. The default path makes no model calls and costs nothing beyond your machine. With a key set, each planned lead makes one call with at most 120 output tokens, and the prompt is the screened form fields (usually under 300 input tokens). Multiply your monthly lead count by your provider's current per-token price to estimate. The evals never call a model.

## Swap the stack

| Piece here | Swap for | Keep |
|---|---|---|
| Mock CRM HTTP service | HubSpot or Salesforce adapter (read-only first) | the `/crm/leads` shape, If-Match/version check, Idempotency-Key |
| Mock Slack | Slack chat.postMessage via a bot token in a secret | drafts behind the same approval |
| SQLite on a volume | Postgres, with the same ops ledger table | one transaction per step |
| Approval CLI | Slack button or internal tool that writes the same record | full-hash binding, approver list, TTL |
| Compose | Kubernetes Deployment, ECS task or Cloud Run job | non-root, read-only root, secrets as files, SIGTERM grace, liveness probe on the healthcheck |
| Template summary | Claude or another model | validator and fallback; router never reads it |

## Customise for your stack

- Territories, segments, free-mail and disposable domains: `config/routing_rules.json` (bump `version`, add a cases_v2 file).
- Reps and who is active: `config/reps.json`. Approvers: `config/approvers.json`.
- Retry and timing: env vars `MAX_ATTEMPTS`, `BACKOFF_BASE_S`, `BACKOFF_CAP_S`, `RATE_LIMIT_PER_S`, `POLL_INTERVAL_S`, `HEARTBEAT_MAX_AGE_S`, `APPROVAL_TTL_H`.

## Production gaps

- The image has not been built or run by me yet; run `docker compose up --build` and `tools/image_secret_scan.py` before relying on the container claims.
- One worker only. Two replicas would need a lease per lead (e.g. a row lock in Postgres).
- No real CRM connector and no live Slack. HubSpot and Salesforce concurrency semantics differ from the mock's integer version.
- Compose (non-swarm) secrets are host bind mounts; production needs a secrets manager and rotation.
- No image signing, SBOM or vulnerability scan in CI yet; the digest pin needs scheduled updates.
- Healthcheck reports liveness, not correctness; alert on parked ops and needs_review age as well.
- Alias table gaps (held-out H01) and the European thousands separator ("1.200") are not handled; both go to review rather than being guessed.
