# syntax=docker/dockerfile:1
# Multi-stage build for the GTM lead routing worker and its mock CRM.
# Base image pinned by tag AND digest (looked up from Docker Hub on 2026-11-22).
# Update both together, e.g. with Dependabot (package-ecosystem: docker).

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt
COPY runtime/ /app/runtime/
COPY config/ /app/config/
COPY sample_leads.csv sample_accounts.csv /app/
# Compile now so the read-only root filesystem never needs __pycache__ writes.
RUN /opt/venv/bin/python -m compileall -q /app/runtime

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime
LABEL org.opencontainers.image.source="https://github.com/rubianaseem/gtm-agent-docker-runtime" \
      org.opencontainers.image.description="GTM lead routing worker with hash-bound human approval (mock CRM only)"
RUN groupadd --system --gid 10001 agent \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent \
 && mkdir -p /data \
 && chown 10001:10001 /data
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data \
    CONFIG_DIR=/app/config \
    CRM_BASE_URL=http://mock-crm:8088
WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "runtime", "healthcheck"]
ENTRYPOINT ["python", "-m", "runtime"]
CMD ["run"]
