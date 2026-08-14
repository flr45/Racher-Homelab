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

from gateway import FileTailSource, PagerEvent, PushoverClient, detect_station, parse_pdl_line
from push_service import WebPushService
from storage import Storage


DATA_DIR = Path(os.getenv("PAGER_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("PAGER_DB_PATH", str(DATA_DIR / "pager.db"))
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
PASSWORD_HASH_METHOD = os.getenv("PAGER_PASSWORD_HASH_METHOD", "pbkdf2:sha256:600000")


def hash_password(password: str) -> str:
    """Create a portable password hash without requiring hashlib.scrypt.

    Some macOS Python builds linked against LibreSSL do not expose
    hashlib.scrypt. PBKDF2-HMAC-SHA256 is supported by Werkzeug on both the
    local macOS test runtime and the Raspberry Pi/Linux production runtime.
    """
    return generate_password_hash(
        password,
        method=PASSWORD_HASH_METHOD,
        salt_length=16,
    )


def persistent_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return value


app = Flask(__name__)
app.secret_key = os.getenv("PAGER_SECRET_KEY") or persistent_secret(DATA_DIR / "session-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("PAGER_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

storage = Storage(DB_PATH)
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
    return {
        "current_user": g.get("user"),
        "csrf_token": session.get("csrf_token", ""),
    }


def maybe_notify_pushover(message_id: int, event: dict[str, Any]) -> None:
    settings = storage.get_settings()
    if settings.get("pushover_enabled") != "1":
        return
    title = event.get("station") or settings.get("gateway_name", "Pager")
    pushover.send(
        settings.get("pushover_app_token", ""),
        settings.get("pushover_user_key", ""),
        title,
        event.get("message", ""),
    )
    storage.mark_notification_sent(message_id)


def send_web_push_for_event(message_id: int, event: dict[str, Any]) -> None:
    payload = {
        "title": event.get("station") or "Pageralarm",
        "body": event.get("message", ""),
        "message_id": message_id,
        "url": "/",
    }
    for subscription in storage.list_active_push_subscriptions():
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
    message_id = storage.add_message(data)
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


# ---- Authentication ------------------------------------------------------------

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
            user_id = storage.create_user(
                username, display_name, hash_password(password), "admin", None
            )
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
            return redirect(url_for("index"))
    return render_template("login.html", error=error)


@app.post("/logout")
@auth_required()
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---- PWA shell -----------------------------------------------------------------

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


# ---- Alarm APIs: both roles -----------------------------------------------------

@app.get("/api/me")
@auth_required()
def api_me():
    return jsonify({
        "id": g.user["id"],
        "username": g.user["username"],
        "display_name": g.user["display_name"],
        "role": g.user["role"],
        "csrf_token": session["csrf_token"],
        "push_devices": len(storage.list_user_push_subscriptions(g.user["id"])),
    })


@app.get("/api/messages")
@auth_required()
def api_messages():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    return jsonify(storage.list_messages(limit))


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
    storage.upsert_push_subscription(
        g.user["id"], endpoint, p256dh, auth, request.headers.get("User-Agent", "")
    )
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
            web_push.send(sub, {
                "title": "Racher Pager Gateway",
                "body": "Testnotifikation - push virker.",
                "url": "/",
            })
            sent += 1
        except WebPushException as exc:
            if web_push.is_gone(exc):
                storage.delete_push_subscription(sub["endpoint"])
            else:
                app.logger.warning("Push test failed: %s", exc)
    return jsonify({"ok": sent > 0, "sent": sent})


# ---- Admin-only APIs ------------------------------------------------------------

@app.get("/api/status")
@auth_required(admin=True)
def api_status():
    now = datetime.now(timezone.utc)
    return jsonify({
        "name": setting("gateway_name", "Racher Pager Gateway"),
        "hostname": socket.gethostname(),
        "source_mode": setting("source_mode", "mock"),
        "source": source.status,
        "message_count": storage.message_count(),
        "latest_message": storage.latest_message(),
        "uptime_seconds": int((now - started_at).total_seconds()),
        "server_time": now.isoformat(),
    })


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
        payload["pushover_enabled"] = "1" if str(payload["pushover_enabled"]).lower() in {
            "1", "true", "on", "yes"
        } else "0"
    subject = str(payload.get("vapid_subject") or current.get("vapid_subject", "")).strip()
    if subject and not (subject.startswith("mailto:") or subject.startswith("https://")):
        return jsonify({"ok": False, "error": "VAPID subject skal være mailto: eller https://"}), 400
    storage.update_settings(payload)
    return jsonify({"ok": True})


@app.post("/api/mock")
@auth_required(admin=True)
def api_mock():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("message") or "$8 ISL KA MØ M1 + V1 (1+5) Naturbrand-Mark").strip()
    if not text:
        return jsonify({"ok": False, "error": "message is required"}), 400
    raw_line = str(payload.get("raw_line") or text)
    event = parse_pdl_line(raw_line, source="mock") or PagerEvent(message=text, raw_line=raw_line, source="mock")
    event.message = text
    event.station = detect_station(text)
    return jsonify({"ok": True, "id": ingest_event(event)})


@app.post("/api/pushover/test")
@auth_required(admin=True)
def api_pushover_test():
    settings = storage.get_settings()
    try:
        pushover.send(
            settings.get("pushover_app_token", ""),
            settings.get("pushover_user_key", ""),
            "Racher Pager Gateway",
            "Testbesked fra pager-gatewayen.",
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.get("/api/users")
@auth_required(admin=True)
def api_users_get():
    return jsonify(storage.list_users())


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
        user_id = storage.create_user(
            username, display_name, hash_password(password), role, g.user["id"]
        )
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "Brugernavnet findes allerede."}), 409
    return jsonify({"ok": True, "id": user_id})


@app.patch("/api/users/<int:user_id>")
@auth_required(admin=True)
def api_user_update(user_id: int):
    target = storage.get_user(user_id)
    if not target:
        return jsonify({"ok": False, "error": "Brugeren findes ikke."}), 404
    payload = request.get_json(silent=True) or {}
    if "active" in payload:
        active = bool(payload["active"])
        if user_id == g.user["id"] and not active:
            return jsonify({"ok": False, "error": "Du kan ikke deaktivere din egen admin-konto."}), 400
        storage.set_user_active(user_id, active)
    if "password" in payload:
        password = str(payload["password"] or "")
        if len(password) < 10:
            return jsonify({"ok": False, "error": "Adgangskoden skal være mindst 10 tegn."}), 400
        storage.set_user_password_hash(user_id, hash_password(password))
    return jsonify({"ok": True})


@app.get("/api/system/commands")
@auth_required(admin=True)
def api_system_commands():
    return jsonify(storage.list_system_commands())


@app.post("/api/system/commands")
@auth_required(admin=True)
def api_system_command_create():
    action = str((request.get_json(silent=True) or {}).get("action") or "")
    try:
        command_id = storage.queue_system_command(action, g.user["id"])
    except ValueError:
        return jsonify({"ok": False, "error": "Ugyldig systemhandling."}), 400
    return jsonify({"ok": True, "id": command_id, "action": action})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")), debug=False)
