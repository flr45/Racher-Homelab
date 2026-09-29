"""Secure network wrapper for the SMS gateway runtime.

The modem worker and other processes inside the container keep using loopback
without credentials. Remote callers that enqueue SMS messages must authenticate
with the shared bearer token when SMS_GATEWAY_API_TOKEN is configured.
"""

from __future__ import annotations

import hmac
import os

import queued_app as runtime
from flask import jsonify, request

app = runtime.app

# Preserve helpers used by Docker build checks and maintenance scripts.
detect_station_code = runtime.detect_station_code
normalize_phone = runtime.normalize_phone


def _remote_enqueue_authorized() -> bool:
    expected = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
    # modem_reader.py talks to 127.0.0.1 inside this same container. Do not make
    # the local modem queue depend on a network secret.
    if str(request.remote_addr or "") in {"127.0.0.1", "::1"}:
        return True

    # A remote bind (for example Tailscale) must never silently become an
    # unauthenticated SMS-sending capability just because the shared token is
    # missing from the environment.
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
    if not _remote_enqueue_authorized():
        return jsonify(ok=False, auth_configured=bool(expected)), 401
    return jsonify(ok=True, auth_configured=bool(expected))


@app.before_request
def protect_remote_sms_enqueue():
    # POST /api/outgoing is the capability that creates a billable/real SMS.
    # Health, modem claim/complete and existing local administration retain their
    # previous behaviour, so this hardening does not break the modem worker.
    if request.method == "POST" and request.path == "/api/outgoing":
        if not _remote_enqueue_authorized():
            return jsonify(error="unauthorized SMS enqueue"), 401
    return None
