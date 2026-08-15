import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque

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
# Add browser-side hardening without forcing a CSP that would break the existing
# inline application code. HSTS is emitted only when the request arrived as HTTPS
# according to Flask or the proxy header supplied by Cloudflare.
@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto", "")).split(",", 1)[0].strip().lower()
    if request.is_secure or forwarded_proto == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path == "/login" or request.path.startswith("/api/"):
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
_login_lock = threading.Lock()
_login_failures = defaultdict(deque)
_login_blocked_until = {}
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
        for key, limit in _login_keys(username):
            bucket = _prune_login_bucket(key, now)
            bucket.append(now)
            if len(bucket) >= limit:
                _login_blocked_until[key] = now + _LOGIN_BLOCK_SECONDS


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
# dependencies required for live alarm ingestion: SQLite and the logfile tailer.
# "waiting" is healthy for PDL mode because missing hardware/log data is a valid
# state while the appliance is waiting for the scanner.
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
# the original Tailscale source address from Flask, so the private config endpoint
# uses a dedicated shared monitor key rather than trusting request.remote_addr.
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

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

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


training = TrainingStore(DB_PATH, routing, adaptive)
register_training_routes(app, storage, training, auth_required)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")), debug=False)
