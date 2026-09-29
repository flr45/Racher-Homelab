"""Secure network wrapper for the SMS gateway runtime.

The modem worker and other processes inside the container keep using loopback
without credentials. Remote callers that enqueue SMS messages must authenticate
with the shared bearer token when SMS_GATEWAY_API_TOKEN is configured.
"""

from __future__ import annotations

import hmac
import os
import re

import queued_app as runtime
from flask import jsonify, request

app = runtime.app

# Preserve helpers used by Docker build checks and maintenance scripts.
detect_station_code = runtime.detect_station_code
normalize_phone = runtime.normalize_phone


_OUTGOING_STATUS_RE = re.compile(r"^/api/outgoing/\d+$")


def _loopback_request() -> bool:
    return str(request.remote_addr or "") in {"127.0.0.1", "::1"}


def _remote_authenticated() -> bool:
    expected = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
    if _loopback_request():
        return True

    # A remote bind (for example Tailscale) must never silently become an
    # unauthenticated SMS capability just because the shared token is missing.
    if not expected:
        return False

    authorization = str(request.headers.get("Authorization") or "")
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        supplied = str(request.headers.get("X-SMS-Gateway-Token") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@app.get("/api/auth-check")
def sms_gateway_auth_check():
    expected = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
    if not _remote_authenticated():
        return jsonify(ok=False, auth_configured=bool(expected)), 401
    return jsonify(ok=True, auth_configured=bool(expected))


@app.before_request
def protect_remote_sms_api():
    # The host-local web/admin/modem worker keeps the full API over loopback.
    # Remote peers (for example the Pager Pi over Tailscale) receive only the
    # minimum contract required for RIC→SMS: auth-check, enqueue and read-only
    # status for the queue item they already know by id.
    if _loopback_request() or not request.path.startswith("/api/"):
        return None

    allowed_remote = (
        request.path == "/api/auth-check"
        or (request.method == "POST" and request.path == "/api/outgoing")
        or (
            request.method == "GET"
            and _OUTGOING_STATUS_RE.fullmatch(request.path) is not None
        )
    )
    if not allowed_remote:
        return jsonify(error="SMS Gateway API is loopback-only"), 403

    if not _remote_authenticated():
        return jsonify(error="unauthorized SMS Gateway request"), 401
    return None
