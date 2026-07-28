from flask import Blueprint, current_app, jsonify, request, session
from markupsafe import escape

from services.audit_service import append_audit_entry
from services.database_service import open_database
from services.maintenance_service import (
    disable_maintenance,
    enable_maintenance,
    maintenance_status,
)

maintenance_blueprint = Blueprint("maintenance", __name__)


def database():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def current_user():
    return request.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()


def admin_allowed():
    user = current_user()
    allowed = current_app.config["ALLOWED_EMAILS"]
    return (
        current_app.config["ADMIN_ACTIONS_ENABLED"]
        and bool(user)
        and (not allowed or user in allowed)
    )


def csrf_valid():
    return bool(session.get("csrf_token")) and (
        request.headers.get("X-CSRF-Token") == session.get("csrf_token")
    )


def write_audit(action, success, message):
    append_audit_entry(
        action,
        "maintenance-mode",
        success,
        message,
        current_user() or "unknown",
        database,
    )


def _unauthorized():
    return jsonify({"error": "Admin-godkendelse og gyldig sikkerhedstoken kræves."}), 403


@maintenance_blueprint.get("/api/maintenance")
def get_maintenance():
    return jsonify({"maintenance": maintenance_status(database)})


@maintenance_blueprint.post("/api/maintenance")
def activate_maintenance():
    if not admin_allowed() or not csrf_valid():
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    try:
        status = enable_maintenance(
            payload.get("message"),
            payload.get("duration_minutes", 60),
            current_user(),
            database,
        )
    except (TypeError, ValueError) as exc:
        write_audit("maintenance-enable", False, str(exc))
        return jsonify({"error": str(exc)}), 400
    write_audit(
        "maintenance-enable",
        True,
        f"Aktiveret til {status['expires_at']}",
    )
    return jsonify({"maintenance": status}), 201


@maintenance_blueprint.delete("/api/maintenance")
def deactivate_maintenance():
    if not admin_allowed() or not csrf_valid():
        return _unauthorized()
    status = disable_maintenance(current_user(), database)
    write_audit("maintenance-disable", True, "Deaktiveret manuelt")
    return jsonify({"maintenance": status})


def _banner(status):
    message = escape(status["message"] or "Vedligeholdelse")
    expires = escape(status["expires_at"] or "")
    return (
        '<section style="margin-bottom:15px;padding:16px 18px;border:1px solid '
        'rgba(255,200,87,.45);border-radius:16px;background:rgba(255,200,87,.12)">'
        '<strong style="color:#ffc857">Vedligeholdelsestilstand aktiv</strong>'
        f'<div style="margin-top:6px">{message}</div>'
        f'<small>Udløber automatisk: {expires}</small></section>'
    )


def init_maintenance(app):
    app.register_blueprint(maintenance_blueprint)

    @app.before_request
    def enforce_maintenance():
        if request.path == "/health" or request.path.startswith("/api/maintenance"):
            return None
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        status = maintenance_status(database)
        if status["enabled"] and not admin_allowed():
            return (
                jsonify(
                    {
                        "error": "Vedligeholdelsestilstand er aktiv.",
                        "maintenance": status,
                    }
                ),
                503,
            )
        return None

    @app.after_request
    def expose_maintenance(response):
        status = maintenance_status(database)
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            payload["maintenance"] = status
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and status["enabled"] and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            response.set_data(html.replace("<main>", "<main>" + _banner(status), 1))
            response.content_length = len(response.get_data())
        return response
