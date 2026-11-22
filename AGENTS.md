# AGENTS.md

Rules for Cursor, Codex CLI, Grok and any other coding assistant. Claude Code reads CLAUDE.md, which has the same rules.

What this repo is: a containerised inbound lead routing worker. It plans owner changes, tasks and Slack drafts, and executes them only against the bundled mock CRM and mock Slack after a named human approves the plan's full SHA-256. It must keep running with the standard library only, with no API key, and without Docker.

Do
- Keep every hard control deterministic and covered by a unit test or a failure-suite scenario.
- Put new labelled cases in a new versioned file (cases_v2.json) and report before and after numbers from `python run_evals.py`.
- Keep `python tools/container_lint.py` at 0 errors. If you change the base image, update the tag and the digest together in both FROM lines.
- Label mocked behaviour as mocked. Say plainly whether an image was actually built.

Do not
- Add a real CRM write, a real send, or an auto-approve flag. Do not let the worker write approval files.
- Put secrets in ARG, ENV, compose `environment`, logs, the state volume or the repo.
- Let free text (message, title, company) reach the router, or quarantined text reach templates or models.
- Use shell-form ENTRYPOINT/CMD, run as root, or remove `read_only`.
- Edit `evals/cases_v1.json` labels. Add emojis.

Commands
```bash
python run_demo.py
python run_evals.py
python -m unittest discover -s tests -t .
python -m evals.failure_suite
python tools/container_lint.py
```
