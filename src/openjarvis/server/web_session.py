"""Browser sessions and brute-force throttling for the instance web UI.

The personal link (``/#key=...``) is exchanged once for an HttpOnly cookie
(``POST /api/session``), so the key leaves the URL bar and is never kept
where page scripts could read it.  The cookie is an HMAC of an expiry date
keyed by the API key: rotating the key invalidates every session.

Failed authentications are counted per client IP; past the limit the IP gets
429s for a while.  On Railway the client IP comes from ``X-Real-IP`` (set by
the edge proxy) -- the first ``X-Forwarded-For`` hop is client-controlled.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from typing import Dict, List

SESSION_COOKIE = "jarvis_session"
SESSION_TTL_S = 30 * 24 * 3600
# Non-GET requests authenticated by cookie must carry this header: a
# cross-site form can't set it (CSRF defence on top of SameSite=Lax).
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "jarvis"


def client_ip(request) -> str:  # noqa: ANN001
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # The last hop was added by the proxy in front of us.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def _key(api_key: str) -> bytes:
    return hashlib.sha256(b"jarvis-session:" + api_key.encode("utf-8")).digest()


def issue_session(api_key: str, now: float | None = None) -> str:
    expires = str(int((now or time.time()) + SESSION_TTL_S))
    sig = hmac.new(_key(api_key), expires.encode(), hashlib.sha256).digest()
    return expires + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def session_valid(token: str, api_key: str, now: float | None = None) -> bool:
    if not token or not api_key:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    good = hmac.new(_key(api_key), expires.encode(), hashlib.sha256).digest()
    good_b64 = base64.urlsafe_b64encode(good).decode().rstrip("=")
    return hmac.compare_digest(sig.encode(), good_b64.encode())


class FailureLimiter:
    """Block a client after too many failed authentications."""

    def __init__(
        self, max_failures: int = 20, window_s: float = 600, block_s: float = 900
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.block_s = block_s
        self._failures: Dict[str, List[float]] = {}
        self._blocked: Dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            until = self._blocked.get(ip, 0)
            if until and until < time.time():
                self._blocked.pop(ip, None)
                return False
            return bool(until)

    def fail(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < self.window_s]
            recent.append(now)
            if len(recent) >= self.max_failures:
                self._blocked[ip] = now + self.block_s
                recent = []
            self._failures[ip] = recent
            if len(self._failures) > 10_000:  # bound memory under a flood
                self._failures.clear()

    def succeed(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


__all__ = [
    "CSRF_HEADER",
    "CSRF_VALUE",
    "SESSION_COOKIE",
    "SESSION_TTL_S",
    "FailureLimiter",
    "client_ip",
    "issue_session",
    "session_valid",
]
