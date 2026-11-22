"""gtm-agent-docker-runtime: a containerised inbound lead routing worker.

Everything here runs with the Python standard library only. The mock CRM and
mock Slack services are local fakes; the worker never writes to a real CRM and
never sends a real message.
"""

__version__ = "1.0.0"
