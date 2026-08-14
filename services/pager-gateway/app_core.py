from __future__ import annotations

import hmac
import os
import re
import secrets
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import (
    Flask, g, jsonify, redirect, render_template, request, send_from_directory,
    session, url_for,
)
from pywebpush import WebPushException
from werkzeug.security import check_password_hash, generate_password_hash

from adaptive import AdaptiveFilter
from adaptive_routes import register_adaptive_routes
from gateway import FileTailSource, PagerEvent, PushoverClient, detect_station, parse_pdl_line, public_message
from push_service import WebPushService
from routing import RoutingStore
from storage import Storage


DATA_DIR = Path(os.getenv("PAGER_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("PAGER_DB_PATH", str(DATA_DIR / "pager.db"))
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
PASSWORD_HASH_METHOD = os.getenv("PAGER_PASSWORD_HASH_METHOD", "pbkdf2:sha256:600000")


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD, salt_length=16)


def persistent_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return value


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


app = Flask(__name__)
app.secret_key = os.getenv("PAGER_SECRET_KEY") or persistent_secret(DATA_DIR / "session-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("PAGER_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

storage = Storage(DB_PATH)
routing = RoutingStore(DB_PATH)
adaptive = AdaptiveFilter(DB_PATH)
pushover = PushoverClient()
started_at = datetime.now(timezone.utc)


def setting(name: str, default: str = "") -> str:
    return storage.get_setting(name, default)


web_push = WebPushService(
    DATA_DIR,
    lambda: setting("vapid_subject", os.getenv("PAGER_VAPID_SUBJECT", "mailto:admin@racher.local")),
)


def api_request() -> bool:
    return request.path.startswith("/api/")


def auth_required(admin: bool = False):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not g.user:
                if api_request():
                    return jsonify({"ok": False, "error": "login required"}), 401
                return redirect(url_for("login"))
            if admin and g.user["role"] != "admin":
                if api_request():
                    return jsonify({"ok": False, "error": "admin required"}), 403
                return redirect(url_for("index"))
            return fn(*args, **kwargs)
        return wrapped
    return decorator


@app.before_request
def load_identity_and_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    user_id = session.get("user_id")
    g.user = storage.get_user(int(user_id)) if user_id else None
    if g.user and not g.user.get("active"):
        session.clear()
        session["csrf_token"] = secrets.token_urlsafe(32)
        g.user = None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            if api_request():
                return jsonify({"ok": False, "error": "invalid CSRF token"}), 400
            return "Invalid CSRF token", 400


@app.context_processor
def template_globals():
    return {"current_user": g.get("user"), "csrf_token": session.get("csrf_token", "")}


def duplicate_window_seconds() -> int:
    try:
        return max(1, min(int(setting("duplicate_window_seconds", "30")), 300))
    except ValueError:
        return 30


def maybe_notify_pushover(message_id: int, event: dict[str, Any]) -> None:
    if not event.get("delivery_eligible", True):
        return
    settings = storage.get_settings()
    if settings.get("pushover_enabled") != "1":
        return
    pushover.send(
        settings.get("pushover_app_token", ""),
        settings.get("pushover_user_key", ""),
        event.get("station") or settings.get("gateway_name", "Pager"),
        public_message(event.get("message", "")),
    )
    storage.mark_notification_sent(message_id)


def send_web_push_for_event(message_id: int, event: dict[str, Any]) -> None:
    if not event.get("delivery_eligible", True):
        return
    payload = {
        "title": event.get("station") or "Pageralarm",
        "body": public_message(event.get("message", "")),
        "message_id": message_id,
        "url": "/",
    }
    subscriptions = routing.list_push_subscriptions_for_event(
        event.get("station"), bool(event.get("delivery_eligible", True))
    )
    for subscription in subscriptions:
        try:
            web_push.send(subscription, payload)
        except WebPushException as exc:
            if web_push.is_gone(exc):
                storage.delete_push_subscription(subscription["endpoint"])
            else:
                app.logger.warning("Web Push failed for subscription %s: %s", subscription["id"], exc)
        except Exception as exc:
            app.logger.warning("Web Push failed for subscription %s: %s", subscription["id"], exc)


def ingest_event(event: PagerEvent) -> int:
    data = event.to_dict()
    data["message"] = public_message(data.get("message", ""))

    if setting("adaptive_filter_enabled", "1") == "1":
        decision = adaptive.evaluate(
            data["message"], data["received_at"], duplicate_window_seconds()
        )
        data.update(decision)
    else:
        data.update({
            "message_fingerprint": adaptive.exact_signature(data["message"]),
            "relevance_class": "unknown",
            "relevance_score": 1.0,
            "suppressed_reason": None,
            "duplicate_of": None,
            "delivery_eligible": True,
            "decision_reason": "adaptivt filter deaktiveret",
        })

    station, routing_source = routing.classify(
        data.get("ric"), data.get("station"), data.get("message", "")
    )
    data["station"] = station
    data["routing_source"] = routing_source
    message_id = storage.add_message(data)
    adaptive.observe(message_id, data["message"])

    if data.get("delivery_eligible", True):
        try:
            maybe_notify_pushover(message_id, data)
        except Exception as exc:
            app.logger.warning("Pushover failed for message %s: %s", message_id, exc)
        threading.Thread(
            target=send_web_push_for_event,
            args=(message_id, data),
            name=f"web-push-{message_id}",
            daemon=True,
        ).start()
    return message_id


def on_pdl_line(line: str) -> None:
    event = parse_pdl_line(line, source="pdl-file")
    if event:
        ingest_event(event)


source = FileTailSource(lambda: setting("pdl_log_path", "/data/pdl.log"), on_pdl_line)
source.start()


def _runtime_flat() -> tuple[dict[str, str], dict[str, str]]:
    rows = storage.get_runtime_status()
    values = {key: item.get("value", "") for key, item in rows.items()}
    updated = {key: item.get("updated_at", "") for key, item in rows.items()}
    return values, updated


def _iso_age_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        return None


def _readiness(runtime: dict[str, str]) -> list[dict[str, str]]:
    heartbeat_age = _iso_age_seconds(runtime.get("agent_heartbeat", ""))
    agent_online = heartbeat_age is not None and heartbeat_age <= 30
    pdl_installed = runtime.get("pdl_installed") == "1"
    pdl_active = runtime.get("pdl_service") == "active"
    fsk_connected = runtime.get("fsk_usb_connected") == "1"
    fsk_in_use = runtime.get("fsk_usb_pdl_in_use") == "1"
    internet_online = runtime.get("internet_online") == "1"
    hotspot_active = runtime.get("hotspot_active") == "1"
    tunnel_installed = runtime.get("tunnel_installed") == "1"
    tunnel_active = runtime.get("tunnel_service") == "active"
    try:
        pdl_log_size = int(runtime.get("pdl_log_size", "0") or 0)
    except ValueError:
        pdl_log_size = 0

    network_detail = "Netværksstatus kommer fra Pi'en"
    if agent_online:
        if internet_online:
            network_detail = f"Online · {runtime.get('wifi_connection') or 'netværk'} · {runtime.get('wifi_ip') or 'IP afventer'}"
        elif hotspot_active:
            network_detail = f"Fallback aktiv: {runtime.get('hotspot_ssid') or 'Racher-Pager-Setup'}"
        else:
            network_detail = "Offline · fallback-hotspot starter automatisk efter timeout"
    tunnel_detail = (
        "Cloudflare Tunnel kører" if tunnel_active else
        "Cloudflare er installeret, men tunnelen er ikke aktiv" if tunnel_installed else
        "Konfigureres når tunnel-token og offentligt hostname er klar"
    )
    if fsk_connected:
        fsk_detail = runtime.get("fsk_usb_summary") or runtime.get("fsk_usb_device") or "FSK-USB fundet"
        if fsk_in_use:
            fsk_detail += " · PDL har enheden åben"
    else:
        fsk_detail = "Tilslut discriminator.nl FSK-USB når hardware er tilgængelig"

    return [
        {"key": "gateway", "label": "Pager Gateway", "state": "ok", "detail": "Webtjenesten og databasen svarer"},
        {"key": "system-agent", "label": "System-agent", "state": "ok" if agent_online else "pending", "detail": "Host-status modtages" if agent_online else "Installeres/aktiveres på Raspberry Pi"},
        {"key": "network", "label": "Netværk", "state": "ok" if internet_online else "pending", "detail": network_detail},
        {"key": "tunnel", "label": "Remote tunnel", "state": "ok" if tunnel_active else "pending", "detail": tunnel_detail},
        {"key": "pdl-installed", "label": "PDL decoder", "state": "ok" if pdl_installed else "pending", "detail": "PDL 3.2.0 er installeret" if pdl_installed else "PDL installeres af Pi-bootstrap"},
        {"key": "pdl-service", "label": "PDL service", "state": "ok" if pdl_active else "pending", "detail": "Decoder-service kører" if pdl_active else "Kan afvente FSK-USB/scanner før live-test"},
        {"key": "fsk-usb", "label": "FSK-USB", "state": "ok" if fsk_connected else "pending", "detail": fsk_detail},
        {"key": "pdl-data", "label": "PDL data", "state": "ok" if pdl_log_size > 0 else "pending", "detail": "PDL har skrevet modtagne data" if pdl_log_size > 0 else "Afventer første rigtige dekodning fra scanner"},
        {"key": "backup", "label": "Backup", "state": "ok" if runtime.get("backup_latest") else "pending", "detail": f"Seneste backup: {runtime.get('backup_latest')}" if runtime.get("backup_latest") else "Første backup oprettes under Pi-installationen"},
    ]


def _station_keys_from_payload(payload: dict[str, Any]) -> list[str]:
    supplied = payload.get("stations")
    if isinstance(supplied, list):
        return routing.normalize_station_keys(supplied)
    selected = []
    for station in routing.list_stations(active_only=True):
        if as_bool(payload.get(f"station_{station['key']}"), False):
            selected.append(station["key"])
    return routing.normalize_station_keys(selected)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if storage.user_count() > 0:
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip() or username
        password = request.form.get("password", "")
        if not USERNAME_RE.fullmatch(username):
            error = "Brugernavn skal være 3-40 tegn og må bruge bogstaver, tal, punktum, _ og -."
        elif len(password) < 10:
            error = "Adgangskoden skal være mindst 10 tegn."
        else:
            user_id = storage.create_user(username, display_name, hash_password(password), "admin", None)
            routing.set_user_receive_all(user_id, True)
            storage.add_audit(user_id, "first-admin", "Første administrator oprettet")
            session.clear()
            session["user_id"] = user_id
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            storage.touch_login(user_id)
            return redirect(url_for("index"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if storage.user_count() == 0:
        return redirect(url_for("setup"))
    if g.user:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user = storage.get_user_by_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        if not user or not user.get("active") or not check_password_hash(user["password_hash"], password):
            error = "Forkert brugernavn eller adgangskode."
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            storage.touch_login(user["id"])
            storage.add_audit(user["id"], "login", "Web login")
            return redirect(url_for("index"))
    return render_template("login.html", error=error)


@app.post("/logout")
@auth_required()
def logout():
    storage.add_audit(g.user["id"], "logout", "Web logout")
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@auth_required()
def index():
    return render_template("index.html", is_admin=g.user["role"] == "admin")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/me")
@auth_required()
def api_me():
    return jsonify({
        "id": g.user["id"], "username": g.user["username"],
        "display_name": g.user["display_name"], "role": g.user["role"],
        "csrf_token": session["csrf_token"],
        "push_devices": len(storage.list_user_push_subscriptions(g.user["id"])),
        "stations": routing.user_stations(g.user["id"]),
        "receive_all": routing.user_receive_all(g.user["id"]),
    })


@app.get("/api/messages")
@auth_required()
def api_messages():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    if g.user["role"] == "admin":
        return jsonify(storage.list_messages(limit))
    return jsonify(routing.list_messages_for_user(g.user["id"], limit))


@app.get("/api/push/vapid-public-key")
@auth_required()
def api_push_public_key():
    return jsonify({"public_key": web_push.public_key})


@app.post("/api/push/subscribe")
@auth_required()
def api_push_subscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint") or "").strip()
    keys = payload.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "invalid push subscription"}), 400
    storage.upsert_push_subscription(g.user["id"], endpoint, p256dh, auth, request.headers.get("User-Agent", ""))
    return jsonify({"ok": True})


@app.post("/api/push/unsubscribe")
@auth_required()
def api_push_unsubscribe():
    endpoint = str((request.get_json(silent=True) or {}).get("endpoint") or "").strip()
    if endpoint:
        storage.delete_push_subscription(endpoint, g.user["id"])
    return jsonify({"ok": True})


@app.post("/api/push/test")
@auth_required()
def api_push_test():
    subscriptions = storage.list_user_push_subscriptions(g.user["id"])
    if not subscriptions:
        return jsonify({"ok": False, "error": "Ingen notifikationsenhed er registreret."}), 400
    sent = 0
    for sub in subscriptions:
        try:
            web_push.send(sub, {"title": "Racher Pager Gateway", "body": "Testnotifikation - push virker.", "url": "/"})
            sent += 1
        except WebPushException as exc:
            if web_push.is_gone(exc):
                storage.delete_push_subscription(sub["endpoint"])
    return jsonify({"ok": sent > 0, "sent": sent})


@app.get("/api/status")
@auth_required(admin=True)
def api_status():
    now = datetime.now(timezone.utc)
    runtime, runtime_updated = _runtime_flat()
    return jsonify({
        "name": setting("gateway_name", "Racher Pager Gateway"),
        "hostname": runtime.get("host_hostname") or socket.gethostname(),
        "source_mode": setting("source_mode", "mock"),
        "source": source.status,
        "message_count": storage.message_count(),
        "latest_message": storage.latest_message(),
        "uptime_seconds": int((now - started_at).total_seconds()),
        "server_time": now.isoformat(),
        "runtime": runtime,
        "runtime_updated_at": runtime_updated,
        "readiness": _readiness(runtime),
        "adaptive": adaptive.stats(),
    })


@app.get("/api/audit")
@auth_required(admin=True)
def api_audit():
    return jsonify(storage.list_audit(limit=50))


@app.get("/api/settings")
@auth_required(admin=True)
def api_settings_get():
    settings = storage.get_settings()
    settings["pushover_app_token_set"] = bool(settings.get("pushover_app_token"))
    settings["pushover_user_key_set"] = bool(settings.get("pushover_user_key"))
    settings["pushover_app_token"] = ""
    settings["pushover_user_key"] = ""
    return jsonify(settings)


@app.post("/api/settings")
@auth_required(admin=True)
def api_settings_post():
    payload = request.get_json(silent=True) or {}
    current = storage.get_settings()
    for secret in ("pushover_app_token", "pushover_user_key"):
        if secret in payload and not str(payload[secret]).strip():
            payload[secret] = current.get(secret, "")
    if "pushover_enabled" in payload:
        payload["pushover_enabled"] = "1" if as_bool(payload["pushover_enabled"]) else "0"
    if "adaptive_filter_enabled" in payload:
        payload["adaptive_filter_enabled"] = "1" if as_bool(payload["adaptive_filter_enabled"]) else "0"
    if "duplicate_window_seconds" in payload:
        try:
            payload["duplicate_window_seconds"] = str(max(1, min(int(payload["duplicate_window_seconds"]), 300)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Dubletvinduet skal være 1-300 sekunder."}), 400
    subject = str(payload.get("vapid_subject") or current.get("vapid_subject", "")).strip()
    if subject and not (subject.startswith("mailto:") or subject.startswith("https://")):
        return jsonify({"ok": False, "error": "VAPID subject skal være mailto: eller https://"}), 400
    storage.update_settings(payload)
    storage.add_audit(g.user["id"], "settings-update", "Gateway-indstillinger ændret")
    return jsonify({"ok": True})


@app.post("/api/mock")
@auth_required(admin=True)
def api_mock():
    payload = request.get_json(silent=True) or {}
    text = public_message(str(payload.get("message") or "$8 ISL KA MØ M1 + V1 (1+5) Naturbrand-Mark").strip())
    if not text:
        return jsonify({"ok": False, "error": "message is required"}), 400
    raw_line = str(payload.get("raw_line") or text)
    event = parse_pdl_line(raw_line, source="mock") or PagerEvent(message=text, raw_line=raw_line, source="mock")
    event.message = text
    event.station = detect_station(text)
    if payload.get("ric"):
        try:
            event.ric = routing.normalize_ric(payload.get("ric"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "id": ingest_event(event)})


@app.post("/api/pushover/test")
@auth_required(admin=True)
def api_pushover_test():
    settings = storage.get_settings()
    try:
        pushover.send(
            settings.get("pushover_app_token", ""), settings.get("pushover_user_key", ""),
            "Racher Pager Gateway", "Testbesked fra pager-gatewayen.",
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.get("/api/stations")
@auth_required(admin=True)
def api_stations():
    return jsonify(routing.list_stations())


@app.get("/api/ric-codes")
@auth_required(admin=True)
def api_ric_codes_get():
    return jsonify(routing.list_ric_codes())


@app.get("/api/ric-codes/unknown")
@auth_required(admin=True)
def api_unknown_ric_codes():
    return jsonify(routing.list_unknown_rics(limit=100))


@app.post("/api/ric-codes")
@auth_required(admin=True)
def api_ric_codes_create():
    payload = request.get_json(silent=True) or {}
    try:
        ric_id = routing.create_ric_code(
            payload.get("ric"), payload.get("station_key"), payload.get("label", ""),
            as_bool(payload.get("active"), True), g.user["id"],
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "RIC-koden findes allerede."}), 409
    row = routing.get_ric_code(ric_id)
    storage.add_audit(g.user["id"], "ric-create", f"ric={row['ric']}; station={row['station_key']}")
    return jsonify({"ok": True, "id": ric_id, "ric": row})


@app.patch("/api/ric-codes/<int:ric_id>")
@auth_required(admin=True)
def api_ric_code_update(ric_id: int):
    payload = request.get_json(silent=True) or {}
    kwargs: dict[str, Any] = {}
    for key in ("ric", "station_key", "label"):
        if key in payload:
            kwargs[key] = payload[key]
    if "active" in payload:
        kwargs["active"] = as_bool(payload["active"])
    try:
        row = routing.update_ric_code(ric_id, **kwargs)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "RIC-koden findes allerede."}), 409
    if not row:
        return jsonify({"ok": False, "error": "RIC-koden findes ikke."}), 404
    storage.add_audit(g.user["id"], "ric-update", f"ric={row['ric']}; station={row['station_key']}")
    return jsonify({"ok": True, "ric": row})


@app.delete("/api/ric-codes/<int:ric_id>")
@auth_required(admin=True)
def api_ric_code_delete(ric_id: int):
    row = routing.get_ric_code(ric_id)
    if not row:
        return jsonify({"ok": False, "error": "RIC-koden findes ikke."}), 404
    routing.delete_ric_code(ric_id)
    storage.add_audit(g.user["id"], "ric-delete", f"ric={row['ric']}; station={row['station_key']}")
    return jsonify({"ok": True})


@app.get("/api/users")
@auth_required(admin=True)
def api_users_get():
    return jsonify(routing.attach_user_stations(storage.list_users()))


@app.post("/api/users")
@auth_required(admin=True)
def api_users_create():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    display_name = str(payload.get("display_name") or username).strip()
    password = str(payload.get("password") or "")
    role = str(payload.get("role") or "user")
    if not USERNAME_RE.fullmatch(username):
        return jsonify({"ok": False, "error": "Ugyldigt brugernavn."}), 400
    if len(password) < 10:
        return jsonify({"ok": False, "error": "Adgangskoden skal være mindst 10 tegn."}), 400
    if role not in {"user", "admin"}:
        return jsonify({"ok": False, "error": "Ugyldig rolle."}), 400
    try:
        stations = _station_keys_from_payload(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    receive_all = as_bool(payload.get("receive_all"), role == "admin")
    try:
        user_id = storage.create_user(username, display_name, hash_password(password), role, g.user["id"])
        routing.set_user_stations(user_id, stations)
        routing.set_user_receive_all(user_id, receive_all)
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "Brugernavnet findes allerede."}), 409
    storage.add_audit(g.user["id"], "user-routing", f"user_id={user_id}; all={int(receive_all)}; stations={','.join(stations) or '-'}")
    return jsonify({"ok": True, "id": user_id, "stations": stations, "receive_all": receive_all})


@app.patch("/api/users/<int:user_id>")
@auth_required(admin=True)
def api_user_update(user_id: int):
    target = storage.get_user(user_id)
    if not target:
        return jsonify({"ok": False, "error": "Brugeren findes ikke."}), 404
    payload = request.get_json(silent=True) or {}
    changed: list[str] = []
    if "active" in payload:
        active = as_bool(payload["active"])
        if user_id == g.user["id"] and not active:
            return jsonify({"ok": False, "error": "Du kan ikke deaktivere din egen admin-konto."}), 400
        storage.set_user_active(user_id, active)
        changed.append("active")
    if "password" in payload:
        password = str(payload["password"] or "")
        if len(password) < 10:
            return jsonify({"ok": False, "error": "Adgangskoden skal være mindst 10 tegn."}), 400
        storage.set_user_password_hash(user_id, hash_password(password))
        changed.append("password")
    if "stations" in payload:
        if not isinstance(payload["stations"], list):
            return jsonify({"ok": False, "error": "Stationer skal sendes som en liste."}), 400
        try:
            stations = routing.set_user_stations(user_id, payload["stations"])
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        changed.append("stations")
        storage.add_audit(g.user["id"], "user-routing", f"user_id={user_id}; stations={','.join(stations) or '-'}")
    if "receive_all" in payload:
        routing.set_user_receive_all(user_id, as_bool(payload["receive_all"]))
        changed.append("receive_all")
    if changed:
        storage.add_audit(g.user["id"], "user-update", f"user_id={user_id}; fields={','.join(changed)}")
    return jsonify({"ok": True})


@app.get("/api/system/commands")
@auth_required(admin=True)
def api_system_commands():
    return jsonify(storage.list_system_commands())


@app.post("/api/system/commands")
@auth_required(admin=True)
def api_system_command_create():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    command_payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    try:
        command_id = storage.queue_system_command(action, g.user["id"], command_payload)
    except ValueError:
        return jsonify({"ok": False, "error": "Ugyldig systemhandling eller parametre."}), 400
    return jsonify({"ok": True, "id": command_id, "action": action})


register_adaptive_routes(app, storage, routing, adaptive, auth_required)
