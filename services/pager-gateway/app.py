from __future__ import annotations

import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from gateway import FileTailSource


# app_core owns the shared source object, but the production entrypoint needs to
# install the source-mode gate before the tail thread can consume a live line.
# Temporarily suppress FileTailSource.start() during app_core import, then restore
# the method and start the source only after selected_pdl_line is installed.
_original_file_tail_start = FileTailSource.start
FileTailSource.start = lambda self: None
try:
    from app_core import *  # noqa: F401,F403
finally:
    FileTailSource.start = _original_file_tail_start

from training import TrainingStore
from training_routes import register_training_routes


# Bound accidental or hostile JSON/form submissions before Flask buffers them.
# Training accepts substantial pasted logs, so the default is deliberately roomy
# while still preventing an authenticated browser from allocating unbounded RAM.
app.config["MAX_CONTENT_LENGTH"] = max(
    1024 * 1024,
    int(os.getenv("PAGER_MAX_REQUEST_BYTES", str(10 * 1024 * 1024))),
)


# The first-admin setup route checks whether any users exist before creating the
# initial administrator. Gunicorn runs this appliance with one worker and several
# threads, so serialize that whole check-and-create flow to prevent two concurrent
# setup requests from both becoming administrators.
_setup_lock = threading.Lock()
_original_setup_view = app.view_functions["setup"]


def serialized_setup(*args, **kwargs):
    with _setup_lock:
        return _original_setup_view(*args, **kwargs)


app.view_functions["setup"] = serialized_setup


# The public hostname is exposed through Cloudflare while the origin remains HTTP.
# The UI has no inline scripts/styles, so enforce a same-origin CSP rather than the
# previous partial frame-only policy. HSTS is emitted only when the request arrived
# as HTTPS according to Flask or the proxy header supplied by Cloudflare.
@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; worker-src 'self'; manifest-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
    )
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto", "")).split(",", 1)[0].strip().lower()
    if request.is_secure or forwarded_proto == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path in {"/", "/login", "/setup"} or request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# Rate-limit failed logins in-process. The appliance intentionally runs one
# Gunicorn worker with threads, so this covers every web request on the device.
# Three buckets avoid both easy brute force and easy account-lockout attacks:
# client+username (strict), username across clients (looser), and client globally.
_LOGIN_WINDOW_SECONDS = max(60, int(os.getenv("PAGER_LOGIN_WINDOW_SECONDS", "900")))
_LOGIN_BLOCK_SECONDS = max(60, int(os.getenv("PAGER_LOGIN_BLOCK_SECONDS", "900")))
_LOGIN_PAIR_LIMIT = max(3, int(os.getenv("PAGER_LOGIN_PAIR_LIMIT", "5")))
_LOGIN_USER_LIMIT = max(_LOGIN_PAIR_LIMIT, int(os.getenv("PAGER_LOGIN_USER_LIMIT", "20")))
_LOGIN_CLIENT_LIMIT = max(_LOGIN_PAIR_LIMIT, int(os.getenv("PAGER_LOGIN_CLIENT_LIMIT", "30")))
_LOGIN_STATE_MAX_KEYS = max(100, int(os.getenv("PAGER_LOGIN_STATE_MAX_KEYS", "5000")))
_LOGIN_CLEANUP_INTERVAL_SECONDS = 60
_login_lock = threading.Lock()
_login_failures = defaultdict(deque)
_login_blocked_until = {}
_login_last_cleanup = 0.0
_original_login_view = app.view_functions["login"]


def _login_client_id() -> str:
    # Cloudflare Tunnel supplies CF-Connecting-IP. Direct LAN/Tailscale requests
    # fall back to Flask's peer address.
    value = str(request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown").strip()
    return value[:80]


def _login_keys(username: str) -> list[tuple[str, int]]:
    client = _login_client_id()
    user = str(username or "").strip().lower()[:80] or "<empty>"
    return [
        (f"pair:{client}:{user}", _LOGIN_PAIR_LIMIT),
        (f"user:{user}", _LOGIN_USER_LIMIT),
        (f"client:{client}", _LOGIN_CLIENT_LIMIT),
    ]


def _trim_login_state_locked(now: float, *, force: bool = False) -> None:
    global _login_last_cleanup
    if not force and now - _login_last_cleanup < _LOGIN_CLEANUP_INTERVAL_SECONDS and len(_login_failures) <= _LOGIN_STATE_MAX_KEYS:
        return

    cutoff = now - _LOGIN_WINDOW_SECONDS
    for key in list(_login_failures):
        bucket = _login_failures.get(key)
        if bucket is None:
            continue
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        blocked_until = float(_login_blocked_until.get(key) or 0)
        if not bucket and blocked_until <= now:
            _login_failures.pop(key, None)
            _login_blocked_until.pop(key, None)

    # A hostile client can vary usernames/IPs and otherwise create an unbounded
    # number of buckets. Keep the newest activity and evict the oldest bookkeeping
    # entries once the fixed appliance budget is reached.
    overflow = len(_login_failures) - _LOGIN_STATE_MAX_KEYS
    if overflow > 0:
        oldest = sorted(
            _login_failures,
            key=lambda key: max(
                _login_failures[key][-1] if _login_failures[key] else 0.0,
                float(_login_blocked_until.get(key) or 0),
            ),
        )[:overflow]
        for key in oldest:
            _login_failures.pop(key, None)
            _login_blocked_until.pop(key, None)
    _login_last_cleanup = now


def _prune_login_bucket(key: str, now: float) -> deque:
    bucket = _login_failures[key]
    cutoff = now - _LOGIN_WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    return bucket


def _login_retry_after(username: str) -> int:
    now = time.monotonic()
    retry_after = 0
    with _login_lock:
        _trim_login_state_locked(now)
        for key, limit in _login_keys(username):
            blocked_until = float(_login_blocked_until.get(key) or 0)
            if blocked_until > now:
                retry_after = max(retry_after, int(blocked_until - now) + 1)
                continue
            if blocked_until:
                _login_blocked_until.pop(key, None)
            bucket = _prune_login_bucket(key, now)
            if len(bucket) >= limit:
                _login_blocked_until[key] = now + _LOGIN_BLOCK_SECONDS
                retry_after = max(retry_after, _LOGIN_BLOCK_SECONDS)
    return retry_after


def _register_login_failure(username: str) -> None:
    now = time.monotonic()
    with _login_lock:
        _trim_login_state_locked(now)
        for key, limit in _login_keys(username):
            bucket = _prune_login_bucket(key, now)
            bucket.append(now)
            if len(bucket) >= limit:
                _login_blocked_until[key] = now + _LOGIN_BLOCK_SECONDS
        if len(_login_failures) > _LOGIN_STATE_MAX_KEYS:
            _trim_login_state_locked(now, force=True)


def _clear_successful_login(username: str) -> None:
    client = _login_client_id()
    user = str(username or "").strip().lower()[:80] or "<empty>"
    with _login_lock:
        for key in (f"pair:{client}:{user}", f"user:{user}"):
            _login_failures.pop(key, None)
            _login_blocked_until.pop(key, None)


def rate_limited_login(*args, **kwargs):
    if request.method != "POST":
        return _original_login_view(*args, **kwargs)

    username = request.form.get("username", "")
    retry_after = _login_retry_after(username)
    if retry_after:
        return (
            render_template("login.html", error="For mange mislykkede loginforsøg. Prøv igen senere."),
            429,
            {"Retry-After": str(retry_after)},
        )

    response = _original_login_view(*args, **kwargs)
    if session.get("user_id"):
        _clear_successful_login(username)
    else:
        _register_login_failure(username)
    return response


app.view_functions["login"] = rate_limited_login


# The PDL tailer deliberately keeps following the logfile in every mode so its
# file offset stays current. Only forward decoded lines into the live ingest path
# when PDL is the selected source. This prevents simulator mode from generating
# real pager alarms and avoids replaying a backlog when switching back to PDL.
def selected_pdl_line(line: str) -> None:
    if setting("source_mode", "mock") == "pdl-file":
        on_pdl_line(line)


source.on_line = selected_pdl_line
source.start()


# A process can remain alive while an internal dependency has failed. Docker's
# restart policy alone cannot recover that state, so make /healthz reflect the two
# dependencies required by the web ingestion process itself. Host/PDL/FSK health
# is intentionally exposed separately to the external monitor below.
def robust_healthz():
    try:
        with storage.connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        app.logger.exception("Gateway healthcheck: database unavailable")
        return jsonify({"ok": False, "database": "error"}), 503

    source_state = str(source.status.get("state") or "unknown")
    if setting("source_mode", "mock") == "pdl-file" and source_state not in {"waiting", "running"}:
        app.logger.error("Gateway healthcheck: PDL tailer state=%s", source_state)
        return jsonify({"ok": False, "database": "ok", "source": source_state}), 503

    return jsonify({"ok": True, "database": "ok", "source": source_state})


app.view_functions["healthz"] = robust_healthz


# External monitor settings live in the Pager database and are edited only by an
# authenticated admin. The monitoring Pi caches the last known recipient so a
# complete pager power/network failure can still trigger an SMS. Docker NAT hides
# the original Tailscale source address from Flask, so the private endpoints use a
# dedicated shared monitor key rather than trusting request.remote_addr.
_MONITOR_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
_MONITOR_SETTING_KEYS = {
    "external_monitor_enabled",
    "external_monitor_sms_to",
    "external_monitor_failure_threshold",
}


def normalize_monitor_phone(value: str) -> str:
    phone = re.sub(r"[\s()-]", "", str(value or ""))
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if phone and not _MONITOR_PHONE_RE.fullmatch(phone):
        raise ValueError("Fejl-SMS nummeret er ugyldigt.")
    return phone


def monitor_request_allowed() -> bool:
    expected = str(storage.get_setting("external_monitor_access_key", "") or "").strip()
    supplied = str(request.headers.get("X-Pager-Monitor-Key", "") or "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _runtime_values() -> dict[str, str]:
    rows = storage.get_runtime_status()
    return {key: str(item.get("value") or "") for key, item in rows.items()}


def _timestamp_age_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        return None


# The shared monitor key is a machine credential, not a browser-editable setting.
# Even an authenticated admin UI only needs to know whether the key exists.
_original_settings_get = app.view_functions["api_settings_get"]


def monitor_safe_settings_get(*args, **kwargs):
    response = _original_settings_get(*args, **kwargs)
    if isinstance(response, tuple) or getattr(response, "status_code", 200) >= 400:
        return response
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        return response
    payload["external_monitor_access_key_set"] = bool(payload.get("external_monitor_access_key"))
    payload["external_monitor_access_key"] = ""
    return jsonify(payload)


app.view_functions["api_settings_get"] = monitor_safe_settings_get


_original_settings_post = app.view_functions["api_settings_post"]


def monitor_validated_settings_post(*args, **kwargs):
    if not g.user:
        return jsonify({"ok": False, "error": "login required"}), 401
    if g.user.get("role") != "admin":
        return jsonify({"ok": False, "error": "admin required"}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Indstillinger skal sendes som et JSON-objekt."}), 400

    # Never allow the normal settings API to rotate or erase the machine key.
    # request.get_json() is cached by Flask, so mutating this dict also protects
    # the original settings handler that reads the same request body afterwards.
    payload.pop("external_monitor_access_key", None)

    # Partial settings updates must leave monitor state untouched. The browser
    # normally submits the full form, but API clients are allowed to change just
    # one unrelated setting without accidentally disabling the external alarm.
    if any(key in payload for key in _MONITOR_SETTING_KEYS):
        current = storage.get_settings()
        try:
            phone = normalize_monitor_phone(
                payload.get("external_monitor_sms_to", current.get("external_monitor_sms_to", ""))
            )
            threshold = max(
                1,
                min(
                    int(
                        payload.get(
                            "external_monitor_failure_threshold",
                            current.get("external_monitor_failure_threshold", "3"),
                        )
                    ),
                    10,
                ),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc) or "Ugyldig monitor-indstilling."}), 400

        enabled = as_bool(
            payload.get("external_monitor_enabled", current.get("external_monitor_enabled", "0")),
            current.get("external_monitor_enabled", "0") == "1",
        )
        if enabled and not phone:
            return jsonify({"ok": False, "error": "Indtast et telefonnummer før ekstern overvågning aktiveres."}), 400

        payload["external_monitor_enabled"] = "1" if enabled else "0"
        payload["external_monitor_sms_to"] = phone
        payload["external_monitor_failure_threshold"] = str(threshold)

    return _original_settings_post(*args, **kwargs)


app.view_functions["api_settings_post"] = monitor_validated_settings_post


@app.get("/api/external-monitor/config")
def external_monitor_config():
    if not monitor_request_allowed():
        return jsonify({"ok": False, "error": "monitor key required"}), 403
    settings = storage.get_settings()
    return jsonify({
        "ok": True,
        "enabled": settings.get("external_monitor_enabled", "0") == "1",
        "sms_to": settings.get("external_monitor_sms_to", ""),
        "failure_threshold": int(settings.get("external_monitor_failure_threshold", "3") or 3),
        "gateway_name": settings.get("gateway_name", "Racher Pager Gateway"),
    })


@app.get("/api/external-monitor/health")
def external_monitor_health():
    if not monitor_request_allowed():
        return jsonify({"ok": False, "error": "monitor key required"}), 403

    issues: list[str] = []
    try:
        with storage.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        database_state = "ok"
    except Exception:
        database_state = "error"
        issues.append("database")

    source_state = str(source.status.get("state") or "unknown")
    if setting("source_mode", "mock") == "pdl-file" and source_state not in {"waiting", "running"}:
        issues.append("gateway-source")

    runtime = _runtime_values()
    if setting("source_mode", "mock") == "pdl-file":
        heartbeat_age = _timestamp_age_seconds(runtime.get("agent_heartbeat", ""))
        if heartbeat_age is None or heartbeat_age > 60:
            issues.append("host-agent")
        if runtime.get("pdl_service") != "active":
            issues.append("pdl-service")

        # Before commissioning, missing FSK hardware is expected. Once the probe
        # has seen the interface once, a later disconnect or loss of PDL ownership
        # is an outage-worthy condition for the external SMS monitor.
        if runtime.get("fsk_usb_ever_seen") == "1":
            if runtime.get("fsk_usb_connected") != "1":
                issues.append("fsk-usb")
            elif runtime.get("fsk_usb_pdl_in_use") != "1":
                issues.append("fsk-pdl-ownership")

    # A dead Cloudflare service leaves the decoder healthy but makes the public
    # pager hostname unavailable. Only require it on appliances where cloudflared
    # is actually installed, so local/test installations remain valid.
    if runtime.get("tunnel_installed") == "1" and runtime.get("tunnel_service") != "active":
        issues.append("cloudflare-tunnel")

    payload = {
        "ok": not issues,
        "database": database_state,
        "source": source_state,
        "host_agent": runtime.get("agent_heartbeat", ""),
        "pdl_service": runtime.get("pdl_service", ""),
        "fsk_commissioned": runtime.get("fsk_usb_ever_seen", "0") == "1",
        "fsk_connected": runtime.get("fsk_usb_connected", "0") == "1",
        "tunnel_service": runtime.get("tunnel_service", ""),
        "issues": issues,
    }
    return jsonify(payload), (200 if not issues else 503)


training = TrainingStore(DB_PATH, routing, adaptive)
register_training_routes(app, storage, training, auth_required)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")), debug=False)
