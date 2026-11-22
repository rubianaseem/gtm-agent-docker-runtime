"""HTTP client for the (mock) CRM and Slack services.

- Bearer token from a runtime secret; the token is redacted in every log line.
- Client-side rate limit (token bucket).
- Retries: 429 (honours Retry-After, capped), 408, 5xx and connection errors,
  exponential backoff with deterministic jitter. Other 4xx are not retried.
- Writes always carry an Idempotency-Key, so a retry after a lost response
  cannot create a second write.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request

from . import jsonlog

RETRYABLE = {408, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, body: dict | str, attempts: int):
        super().__init__(f"HTTP {status} after {attempts} attempt(s)")
        self.status, self.body, self.attempts = status, body, attempts


class RetriesExhausted(HttpError):
    pass


def backoff_delay(attempt: int, base: float, cap: float, key: str = "") -> float:
    """Full-jitter exponential backoff, made deterministic by hashing the key and attempt."""
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    frac = int(hashlib.sha256(f"{key}:{attempt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round(ceiling * (0.5 + 0.5 * frac), 4)


class TokenBucket:
    def __init__(self, rate_per_s: float, sleep):
        self.rate = max(rate_per_s, 0.1)
        self.tokens = self.rate
        self.last = time.monotonic()
        self.sleep = sleep

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            self.sleep((1 - self.tokens) / self.rate)
            self.tokens = 0
        else:
            self.tokens -= 1


class CrmClient:
    def __init__(self, base_url: str, token: str, settings, stop_event=None):
        self.base = base_url.rstrip("/")
        self._token = token
        self.s = settings
        self.stop_event = stop_event
        self.bucket = TokenBucket(settings.rate_limit_per_s, self._sleep)
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0, "server_errors": 0, "conn_errors": 0,
                      "slept_s": 0.0}

    def _sleep(self, seconds: float):
        seconds = max(0.0, seconds) * self.s.sleep_scale
        self.stats["slept_s"] = round(self.stats["slept_s"] + seconds, 4)
        if self.stop_event is not None:
            self.stop_event.wait(seconds)  # wakes early on SIGTERM
        else:
            time.sleep(seconds)

    def request(self, method: str, path: str, body: dict | None = None, idem_key: str | None = None,
                if_match: int | None = None) -> dict:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        if if_match is not None:
            headers["If-Match"] = str(if_match)
        attempt = 0
        last_status, last_body = 0, ""
        while True:
            attempt += 1
            self.bucket.take()
            self.stats["requests"] += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            retry_after = None
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode() or "{}"
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                last_status = e.code
                try:
                    last_body = json.loads(e.read().decode() or "{}")
                except ValueError:
                    last_body = {"error": "non-json body"}
                if e.code not in RETRYABLE:
                    raise HttpError(e.code, last_body, attempt)
                if e.code == 429:
                    self.stats["rate_limited"] += 1
                    ra = e.headers.get("Retry-After")
                    if ra and ra.strip().isdigit():
                        retry_after = min(float(ra), self.s.retry_after_cap_s)
                else:
                    self.stats["server_errors"] += 1
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                last_status, last_body = 0, {"error": type(e).__name__}
                self.stats["conn_errors"] += 1
            if attempt >= self.s.max_attempts:
                jsonlog.log("error", "http_retries_exhausted", method=method, path=path, status=last_status,
                            attempts=attempt)
                raise RetriesExhausted(last_status, last_body, attempt)
            if self.stop_event is not None and self.stop_event.is_set():
                raise RetriesExhausted(last_status, {"error": "shutdown requested during retry"}, attempt)
            delay = retry_after if retry_after is not None else backoff_delay(
                attempt, self.s.backoff_base_s, self.s.backoff_cap_s, key=f"{method}{path}{idem_key or ''}")
            self.stats["retries"] += 1
            jsonlog.log("warning", "http_retry", method=method, path=path, status=last_status, attempt=attempt,
                        delay_s=delay, retry_after=retry_after is not None)
            self._sleep(delay)
