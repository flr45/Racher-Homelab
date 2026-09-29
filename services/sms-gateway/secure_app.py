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



def _loopback_request() -> bool:
    return str(request.remote_addr or "") in {"127.0.0.1", "::1"}


def _worker_endpoint() -> bool:
    path = str(request.path or "")
    if path == "/api/outgoing/claim":
        return True
    if path.startswith("/api/outgoing/") and (
        path.endswith("/start") or path.endswith("/complete")
    ):
        return True
    if path == "/api/commands/claim":
        return True
    if path.startswith("/api/commands/") and path.endswith("/complete"):
        return True
    return False

def _remote_enqueue_authorized() -> bool:
    expected = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
    if not expected:
        # Backwards compatible while the service remains loopback-only. The
        # deployment guide requires a token before binding the port to Tailscale.
        return True

    # modem_reader.py talks to 127.0.0.1 inside this same container. Do not make
    # the local modem queue depend on a network secret.
    if str(request.remote_addr or "") in {"127.0.0.1", "::1"}:
        return True

    authorization = str(request.headers.get("Authorization") or "")
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        supplied = str(request.headers.get("X-SMS-Gateway-Token") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@app.before_request
def protect_sms_gateway_api():
    # Only modem_reader.py and the local command worker may manipulate the queue
    # lifecycle. The service can be bound to a Tailscale address so the Pager can
    # enqueue SMS, but remote peers must never be able to claim, start or complete
    # someone else's queued message.
    if request.method == "POST" and _worker_endpoint() and not _loopback_request():
        return jsonify(error="worker endpoint is loopback-only"), 403

    # POST /api/outgoing is the one remote capability intentionally exposed to
    # the Pager host. Require the shared bearer token when configured.
    if request.method == "POST" and request.path == "/api/outgoing":
        if not _remote_enqueue_authorized():
            return jsonify(error="unauthorized SMS enqueue"), 401
    return None
