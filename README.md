# GTM Agent Docker Runtime

**A lead routing agent packaged as a container worker. It survives restarts without writing twice, keeps secrets out of the image, stops cleanly on `docker stop`, and changes nothing in the (mock) CRM until a named person approves the exact plan by SHA-256.**

Built by Rubia Naseem, GTM Engineer (12+ yrs CRM/RevOps, HubSpot & Salesforce).

Status: implemented and tested locally on synthetic data, without Docker. The Dockerfile and compose file pass static checks, but I have not built or run the image yet: Docker was not installed on the machine I published from. `tools/image_secret_scan.py` runs that check wherever Docker is available.

## The problem

A routing agent that works in my terminal is not ready to run unattended. Containers get restarted and killed, so a run can stop halfway through a CRM write. Docker's docs name the traps:

- "Build arguments and environment variables are inappropriate for passing secrets to your build, because they persist in the final image." ([Build secrets](https://docs.docker.com/build/building/secrets/))
- With a shell-form ENTRYPOINT, "your executable doesn't receive a `SIGTERM` from `docker stop <container>`." ([Dockerfile reference](https://docs.docker.com/reference/dockerfile/))
- "If you're injecting passwords and API keys as environment variables, you risk unintentional information exposure." ([Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/))

In GTM terms: a lead assigned twice, duplicate tasks and Slack pings, or a rep's manual change overwritten.

## What it does

- **Routes leads deterministically** (open opportunity owner, named account, existing owner, territory and segment, spam, duplicates). Uncertain leads go to a review queue. Free text never picks an owner.
- **Checkpoints every step** to SQLite on a volume and resumes after a kill with the same plan hash.
- **Writes once**: Idempotency-Key and If-Match on every op; backoff, Retry-After and a client-side rate limit.
- **Stops cleanly**: SIGTERM interrupts backoff, commits the checkpoint, exits 0. A heartbeat feeds the HEALTHCHECK.
- **Ships hardened**: multi-stage, digest-pinned base, non-root uid 10001, read-only root, dropped capabilities, compose secrets, internal network, redacted JSON logs and a hash-chained audit trail.

## Where each step runs

1. **GitHub** stores the code, the Dockerfile, the compose file and the synthetic sample data.
2. **Your terminal** (Mac Terminal or the Cursor terminal) runs it, either as plain Python in a venv or with `docker compose up`. No API key is needed.
3. **Optional Claude** writes the rep's one-line context only if you add a key to `secrets/anthropic_api_key.txt` plus the egress override. Bad output falls back to the template.
4. **Real tools**: it writes only to the bundled mock CRM and mock Slack. You approve a plan by hash and make the change in HubSpot or Salesforce yourself. It performs no external writes.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # standard library only
python run_demo.py                       # mock CRM + worker, SIGTERM, approvals, SIGKILL, resume
python run_evals.py                      # routing eval, container checks, failure suite
python -m unittest discover -s tests -t .
python tools/make_dev_secrets.py && docker compose up --build   # with Docker
```

## Example output (outputs/demo_transcript.txt)

```text
  SIGTERM to exit: 0.02s
  L007  pending_approval  assign  R05_TERRITORY_NA_MM  rep.na.mm.1  warnings=quarantined:message:override_instructions,...
  approve L002 as agent:worker with full hash: REFUSED: agent or service identity cannot approve: agent:worker
  first run exit code -9 (SIGKILL)
  {"event": "op_resend_same_key", "lead_id": "L004", "op": "update_lead", "op_index": 0}
  writes 9, duplicate effects 0, tasks 3, mock Slack messages 3
  audit chain: 54 records, chain intact
```

## Results

From `python run_evals.py`, synthetic data:

- Routing, 40 cases: 30/30 dev and 9/10 held-out, against 12/30 and 5/10 for a country-only script. Leads needing a human were held 11/11 times (baseline 0/11). The miss: "U.K." is not in the alias table.
- Failure suite 22/22. A SIGKILL between a committed write and its ack caused 0 duplicates. Restarted from the top, the naive worker wrote 4 extra times (2 exact duplicates); it also duplicated 3 writes when responses were lost and 13 on a plain re-run.
- Static checks: 0 errors here, 29 on the naive fixtures. Planted secret files reaching the build context: 0 of 4 (naive: 4 of 4).

The suite caught one real bug: after a kill mid-execution, the worker read its own write as an outside edit. It is now fixed. Details: [docs/DETAILS.md](docs/DETAILS.md), `evals/report.md`.

## Guardrails and human in the loop

- Approval is enforced in code: a named approver from `config/approvers.json` quotes the full 64-character hash, and it expires after 24 hours. Agent identities are refused and any edit voids it. There is no auto-approve flag.
- If the record changes (412), or a lead is edited while it waits for approval, the plan is voided and re-planned for review.
- Lead text is quarantined before any template or model sees it.

## How this runs today (and what production would add)

Today: a mock CRM, one worker, a SQLite volume. Production would add a read-only CRM connector, a writer behind the same gate, a secrets manager, image scanning and signing in CI, and alerts.

Walk through it line by line: [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

## More

[SETUP.md](SETUP.md) | [Details, costs, stack swaps, production gaps](docs/DETAILS.md) | [Sources](docs/SOURCES.md) | Portfolio plan: GTM Engineering runtime layer under the F1 signal router | Related: [gtm-agent-langfuse-tracing](https://github.com/rubianaseem/gtm-agent-langfuse-tracing) (same territory model, traced), [crm-change-control-plane](https://github.com/rubianaseem/crm-change-control-plane), [gtm-agent-eval-harness](https://github.com/rubianaseem/gtm-agent-eval-harness)
