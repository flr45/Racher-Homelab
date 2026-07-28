import os
import threading
import time
from collections import defaultdict, deque

from flask import jsonify, request

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_DEFAULT_LIMIT = 60
_DEFAULT_WINDOW_SECONDS = 60


class InMemoryRateLimiter:
    def __init__(self, limit=_DEFAULT_LIMIT, window_seconds=_DEFAULT_WINDOW_SECONDS, clock=None):
        self.limit = max(int(limit), 1)
        self.window_seconds = max(int(window_seconds), 1)
        self.clock = clock or time.monotonic
        self._requests = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key):
        now = self.clock()
        threshold = now - self.window_seconds
        with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] <= threshold:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])))
                return False, retry_after
            bucket.append(now)
            return True, 0


def _client_key():
    user = request.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
    address = request.headers.get("CF-Connecting-IP", request.remote_addr or "unknown")
    return f"{user or 'anonymous'}:{address}:{request.endpoint or request.path}"


def _should_rate_limit():
    if request.method not in _SAFE_METHODS:
        return True
    return request.path == "/api/assistant"


def init_security(app):
    app.config.setdefault(
        "RATE_LIMIT_REQUESTS",
        int(os.getenv("RATE_LIMIT_REQUESTS", str(_DEFAULT_LIMIT))),
    )
    app.config.setdefault(
        "RATE_LIMIT_WINDOW_SECONDS",
        int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", str(_DEFAULT_WINDOW_SECONDS))),
    )
    app.extensions["racher_rate_limiter"] = InMemoryRateLimiter(
        app.config["RATE_LIMIT_REQUESTS"],
        app.config["RATE_LIMIT_WINDOW_SECONDS"],
    )

    @app.before_request
    def enforce_rate_limit():
        if not _should_rate_limit():
            return None
        allowed, retry_after = app.extensions["racher_rate_limiter"].check(_client_key())
        if allowed:
            return None
        response = jsonify({"error": "For mange forespørgsler. Prøv igen senere."})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response

    @app.after_request
    def apply_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data:; "
            "font-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
