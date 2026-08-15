import ipaddress
import re
import threading

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
# authenticated admin. The monitoring Pi needs the last known recipient even when
# this appliance later loses power, so it refreshes a local cache while the pager
# is healthy. The config endpoint is intentionally reachable only over Tailscale's
# CGNAT range; Cloudflare/public traffic cannot retrieve the SMS recipient.
_MONITOR_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


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
    if os.getenv("PAGER_MONITOR_ALLOW_LOCAL", "0") == "1" and request.remote_addr in {"127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(request.remote_addr or "") in _TAILSCALE_NETWORK
    except ValueError:
        return False


_original_settings_post = app.view_functions["api_settings_post"]


def monitor_validated_settings_post(*args, **kwargs):
    payload = request.get_json(silent=True) or {}
    try:
        phone = normalize_monitor_phone(payload.get("external_monitor_sms_to", ""))
        threshold = max(1, min(int(payload.get("external_monitor_failure_threshold", "3")), 10))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc) or "Ugyldig monitor-indstilling."}), 400

    enabled = as_bool(payload.get("external_monitor_enabled"), False)
    if enabled and not phone:
        return jsonify({"ok": False, "error": "Indtast et telefonnummer før ekstern overvågning aktiveres."}), 400

    response = _original_settings_post(*args, **kwargs)
    status_code = response[1] if isinstance(response, tuple) and len(response) > 1 else getattr(response, "status_code", 200)
    if int(status_code) < 400:
        storage.update_settings({
            "external_monitor_enabled": "1" if enabled else "0",
            "external_monitor_sms_to": phone,
            "external_monitor_failure_threshold": str(threshold),
        })
    return response


app.view_functions["api_settings_post"] = monitor_validated_settings_post


@app.get("/api/external-monitor/config")
def external_monitor_config():
    if not monitor_request_allowed():
        return jsonify({"ok": False, "error": "Tailscale access required"}), 403
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
