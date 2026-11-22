# Sources

Opened and read on 22 Nov 2026. Quotes in the README were checked against these pages.

Primary docs (Docker):
- Build secrets: https://docs.docker.com/build/building/secrets/ ("Build arguments and environment variables are inappropriate for passing secrets to your build, because they persist in the final image.")
- Dockerfile reference, ENTRYPOINT and HEALTHCHECK: https://docs.docker.com/reference/dockerfile/ (shell-form ENTRYPOINT: "your executable doesn't receive a `SIGTERM` from `docker stop <container>`"; HEALTHCHECK options and defaults: interval 30s, timeout 30s, start period 0s, retries 3)
- Manage secrets in Compose: https://docs.docker.com/compose/how-tos/use-secrets/ ("If you're injecting passwords and API keys as environment variables, you risk unintentional information exposure."; secrets mount at /run/secrets/<name>)
- Building best practices: https://docs.docker.com/build/building/best-practices/ (multi-stage builds; pin base images to a digest; USER for services that can run without privileges; Dependabot for digest updates)
- Compose services reference: https://docs.docker.com/reference/compose-file/services/ (read_only, cap_drop, security_opt, stop_grace_period, tmpfs, depends_on condition service_healthy)
- Base image digest for python:3.12.12-slim-bookworm looked up from the Docker Hub registry API on 22 Nov 2026.

Public posts (X, read as text only, not quoted in the README):
- https://x.com/sunilmehta_si/status/2122565207633625488 (17 Nov 2026): assume prompt injection hijacks the agent and ask what it can reach; no network by default, read-only, non-root.
- https://x.com/orensalami/status/2116944360075825535 (1 Nov 2026): bounded retries that fail closed to human review; non-root container.

GitHub (search results, names and descriptions only; no code reused): termo-ai/termo, Eomaxl/Dockerized-AI-Agent, careem111/ai-agent-lab (Apache-2.0).

My own earlier repos reused as ideas (MIT, same author): the synthetic territory model from gtm-agent-langfuse-tracing; hash-bound approval and the audit chain pattern from hubspot-mcp-slack-approval-agent and crm-change-control-plane.
