import os
import secrets
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    render_template,
    request,
    session,
)

from rbac_extension import current_identity
from services import module_registry_service
from services.app_registry_service import (
    build_app_center,
    load_app_registry,
    resolve_registry_links,
)
from services.audit_service import append_audit_entry
from services.database_service import open_database
from services.docker_service import (
    ContainerNotFoundError,
    docker_status,
    perform_container_action,
)
from services.rbac_service import has_permission

app_center_blueprint = Blueprint("app_center", __name__)
APP_CENTER_MODULE = {
    "id": "app-center",
    "name": "App Center",
    "description": "Deklarativt app-registry, Docker-status, health og live ressourceforbrug.",
    "href": "/apps",
    "category": "overview",
    "permission": "system.read",
    "status_endpoint": "/api/app-center",
}
ACTION_PERMISSIONS = {
    "start": "container.start",
    "restart": "container.restart",
    "stop": "container.stop",
}


def _database():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def _write_audit(action, app_id, success, message, identity):
    append_audit_entry(
        f"app.{action}",
        app_id,
        success,
        message,
        identity.get("email") or identity["role"],
        _database,
    )


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _valid_csrf_token():
    provided = str(request.headers.get("X-CSRF-Token", ""))
    expected = str(session.get("csrf_token", ""))
    return bool(provided and expected) and secrets.compare_digest(provided, expected)


def _find_registered_app(app_id):
    return next(
        (item for item in current_app.config.get("APP_LINKS", []) if item.get("id") == app_id),
        None,
    )


def _allowed_actions(identity, app, installed=True):
    if not current_app.config.get("APP_ACTIONS_ENABLED", False) or not installed:
        return []
    if app.get("category") == "infrastructure" and identity["role"] != "admin":
        return []
    return [
        action
        for action, permission in ACTION_PERMISSIONS.items()
        if has_permission(identity["role"], permission)
    ]


def app_center_report(identity=None):
    containers, docker_error = docker_status(include_usage=True)
    report = build_app_center(
        current_app.config.get("APP_LINKS", []),
        containers,
        registry_errors=current_app.config.get("APP_REGISTRY_ERRORS", []),
        docker_error=docker_error,
    )
    if identity:
        for app in report["apps"]:
            app["allowed_actions"] = _allowed_actions(
                identity,
                app,
                installed=app["installed"],
            )
    report["actions_enabled"] = bool(
        current_app.config.get("APP_ACTIONS_ENABLED", False)
    )
    return report


def _can_read_app_center(identity):
    return has_permission(identity["role"], "system.read")


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app_center_blueprint.get("/apps")
def app_center_page():
    identity = current_identity()
    if not _can_read_app_center(identity):
        return jsonify(
            {
                "error": "Brugeren har ikke adgang til App Center.",
                "required_permission": "system.read",
            }
        ), 403

    return _no_store(
        make_response(
            render_template(
                "app_center.html",
                report=app_center_report(identity),
                actor=identity.get("email") or identity["role"],
                csrf_token=_csrf_token(),
            )
        )
    )


@app_center_blueprint.get("/api/app-center")
def api_app_center():
    identity = current_identity()
    if not _can_read_app_center(identity):
        return jsonify(
            {
                "error": "Brugeren har ikke adgang til App Center.",
                "required_permission": "system.read",
            }
        ), 403

    return _no_store(
        jsonify(
            {
                "app_center": app_center_report(identity),
                "actor": identity.get("email") or identity["role"],
            }
        )
    )


@app_center_blueprint.post("/api/app-center/<app_id>/<action>")
def api_app_action(app_id, action):
    identity = current_identity()
    app = _find_registered_app(app_id)
    if not app:
        return _no_store(jsonify({"error": "Appen findes ikke i registry."})), 404
    if action not in ACTION_PERMISSIONS:
        return _no_store(jsonify({"error": "Ukendt app-handling."})), 400
    if not current_app.config.get("APP_ACTIONS_ENABLED", False):
        return _no_store(jsonify({"error": "App-handlinger er deaktiveret."})), 403
    if action not in _allowed_actions(identity, app):
        return _no_store(
            jsonify(
                {
                    "error": "Brugeren har ikke adgang til handlingen.",
                    "required_permission": ACTION_PERMISSIONS[action],
                }
            )
        ), 403
    if not _valid_csrf_token():
        return _no_store(jsonify({"error": "Ugyldig sikkerhedstoken."})), 403

    payload = request.get_json(silent=True) or {}
    expected_confirmation = f"{action.upper()} {app_id}"
    if payload.get("confirm") != expected_confirmation:
        return _no_store(
            jsonify(
                {
                    "error": "Bekræftelsen matcher ikke.",
                    "required_confirmation": expected_confirmation,
                }
            )
        ), 400

    service = app["service"]
    if service in current_app.config.get("PROTECTED_CONTAINERS", set()):
        _write_audit(action, app_id, False, "Beskyttet container", identity)
        return _no_store(jsonify({"error": "Containeren er beskyttet."})), 409

    try:
        perform_container_action(service, action)
        _write_audit(action, app_id, True, f"Udført på {service}", identity)
        return _no_store(
            jsonify(
                {
                    "ok": True,
                    "app": app_id,
                    "container": service,
                    "action": action,
                }
            )
        )
    except ContainerNotFoundError:
        _write_audit(action, app_id, False, "Container ikke fundet", identity)
        return _no_store(jsonify({"error": "App-containeren blev ikke fundet."})), 404
    except Exception as exc:
        _write_audit(action, app_id, False, type(exc).__name__, identity)
        return _no_store(jsonify({"error": "App-handlingen fejlede."})), 503


def _register_navigation_module():
    if any(item["id"] == APP_CENTER_MODULE["id"] for item in module_registry_service.MODULES):
        return
    dashboard, *remaining = module_registry_service.MODULES
    module_registry_service.MODULES = (
        dashboard,
        dict(APP_CENTER_MODULE),
        *remaining,
    )


def init_app_center(app):
    default_root = Path(app.root_path) / "app_registry"
    registry_root = Path(app.config.get("APP_REGISTRY_ROOT", default_root))
    registry, errors = load_app_registry(registry_root)

    if registry:
        app.config["APP_LINKS"] = resolve_registry_links(
            registry,
            legacy_links=app.config.get("APP_LINKS", []),
        )
    app.config["APP_REGISTRY_ROOT"] = registry_root
    app.config["APP_REGISTRY_ERRORS"] = errors
    app.config.setdefault(
        "APP_ACTIONS_ENABLED",
        os.getenv("APP_ACTIONS_ENABLED", "false").lower() == "true",
    )
    _register_navigation_module()
    app.register_blueprint(app_center_blueprint)
