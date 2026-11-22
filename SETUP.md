# Setup

Everything runs on Python 3.11+ with the standard library. Docker is optional.

## 1. Plain Python in a venv (no Docker, no API key)

```bash
git clone https://github.com/rubianaseem/gtm-agent-docker-runtime.git
cd gtm-agent-docker-runtime
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # nothing to install; kept for the Docker build
python run_demo.py                     # full walk-through, writes outputs/demo_transcript.txt
python run_evals.py                    # writes evals/report.md and evals/results.json
python -m unittest discover -s tests -t .
python tools/container_lint.py
```

## 2. Run the two processes yourself (same as the compose stack)

```bash
python tools/make_dev_secrets.py                       # random token in secrets/crm_token.txt
export CRM_TOKEN_FILE=./secrets/crm_token.txt STATE_DIR=./state/manual
python -m runtime mock --host 127.0.0.1 --port 8088 &  # mock CRM + mock Slack
python -m runtime run                                  # worker loop; Ctrl+C or kill -TERM to stop
python -m runtime queue                                # review queue
python -m runtime show L001                            # full plan with plan_hash
python -m runtime approve L001 --hash <64-hex plan_hash> --approver rubia.naseem
python -m runtime verify-audit
```

The worker picks up the approval on its next poll (5 seconds by default) and executes it against the mock only.

## 3. Docker

Not executed on the machine this repo was published from (Docker was not installed). The commands below follow the Docker docs; please open an issue if anything differs on your machine.

```bash
python tools/make_dev_secrets.py
docker compose up --build -d
docker compose ps                      # worker should become healthy
docker compose logs -f worker          # JSON logs, secrets redacted
docker compose run --rm worker queue
docker compose run --rm worker show L001
docker compose run --rm worker approve L001 --hash <64-hex> --approver rubia.naseem
docker compose stop worker             # SIGTERM; expect worker_stopped with reason stop_signal
docker compose start worker            # resumes from the checkpoint in the agent-state volume
python tools/image_secret_scan.py      # builds with canary secrets and scans history, config and layers
```

Notes:
- `secrets/*.txt` are bind-mounted by Compose with host permissions. The container runs as uid 10001, so `make_dev_secrets.py` sets them to 0644. That is for a dev laptop only; use your platform's secrets manager elsewhere.
- The default network is `internal: true`, so the stack has no internet access. The optional Claude path needs `docker compose -f compose.yaml -f compose.claude.yaml up`.
- To refresh the pinned base image, look up the new digest for the tag and update both `FROM` lines, or let Dependabot (package-ecosystem: docker) raise the PR.

## 4. Optional Claude (template is the default)

Put a key in `secrets/anthropic_api_key.txt` (Docker) or set `ANTHROPIC_API_KEY_FILE` (venv). Optional `ANTHROPIC_MODEL`, default `claude-sonnet-4-5`. The model only writes the one-line context in the task and the Slack draft. Owner choice never reads it, output that fails the validator falls back to the template, and the approver sees the exact text because the plan hash covers it. No eval number in this repo depends on a model call.

## 5. Real data sources

The worker talks to `CRM_BASE_URL` through `runtime/http_client.py`. To point it at your data:

1. Export leads and accounts to `sample_leads.csv` and `sample_accounts.csv` with the same columns and run the mock against them (`python -m runtime mock --leads my_leads.csv --accounts my_accounts.csv`). This is the safest first step.
2. For a live read-only connection, write an adapter that maps HubSpot `GET /crm/v3/objects/contacts` (or a Salesforce SOQL query) to the `/crm/leads` shape, and use a read-only private app or integration user.
3. Keep writes going through the approval gate. Map `update_lead`, `create_task` and `slack_notify` to your APIs only after you have tested against a sandbox portal or org, and keep the Idempotency-Key and version check (HubSpot and Salesforce handle concurrency differently; check their docs).

Edit `config/routing_rules.json`, `config/reps.json` and `config/approvers.json` for your territories, reps and approvers.

## 6. Extending with Cursor, Claude Code, Codex CLI or Grok

Open the repo in your tool. `CLAUDE.md` (Claude Code) and `AGENTS.md` (Cursor, Codex CLI, Grok and others) hold the rules. Good first prompts:
- "Add 'U.K.' and 'GB-ENG' to the alias table, add cases to a new evals/cases_v2.json, and report before and after numbers from python run_evals.py."
- "Add a scenario to the failure suite where the mock returns 409 on create_task, and handle it."
Ask the tool to run the unit tests, `python run_evals.py` and `python tools/container_lint.py` before it finishes.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `startup_refused_missing_secret` and exit 3 | Run `python tools/make_dev_secrets.py` or set `CRM_TOKEN_FILE`. |
| `poll_failed` with status 401 | The worker and mock use different tokens. Restart both after creating secrets. |
| Container unhealthy | `docker compose exec worker python -m runtime healthcheck` prints the reason (stale heartbeat, fatal, unreadable state). |
| `approval_refused: hash_mismatch` | The plan changed after you copied the hash (the lead was edited or re-planned). Run `show` again and review the new plan. |
| `stale_record_refused` | Someone edited the record after approval. The lead is re-planned; approve the new plan if it is right. |
| Permission denied on `/data` | The named volume was created by an older image running as another uid. Create a new volume name in compose.yaml rather than deleting the old one. |
| `cases_v1.json does not match` | Labels were edited. Put new labels in `cases_v2.json`. |
