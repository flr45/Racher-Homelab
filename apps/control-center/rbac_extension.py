import os

from flask import Blueprint, current_app, jsonify, request

from services.rbac_service import has_permission, normalize_email, permissions_for, resolve_role

rbac_blueprint = Blueprint("rbac", __name__)


def _csv(name):
    return {
        normalize_email(value)
        for value in os.getenv(name, "").split(",")
        if normalize_email(value)
    }


def current_identity():
    email = normalize_email(request.headers.get("Cf-Access-Authenticated-User-Email", ""))
    role = resolve_role(
        email,
        admins=current_app.config.get("RBAC_ADMIN_EMAILS", ()),
        operators=current_app.config.get("RBAC_OPERATOR_EMAILS", ()),
        viewers=current_app.config.get("RBAC_VIEWER_EMAILS", ()),
        default_role=current_app.config.get("RBAC_DEFAULT_ROLE", "anonymous"),
    )
    return {
        "authenticated": bool(email),
        "email": email or None,
        "role": role,
        "permissions": permissions_for(role),
    }


@rbac_blueprint.get("/api/identity")
def api_identity():
    return jsonify({"identity": current_identity()})


def init_rbac(app):
    legacy_admins = set(app.config.get("ALLOWED_EMAILS", ()))
    app.config.setdefault("RBAC_ADMIN_EMAILS", legacy_admins | _csv("RBAC_ADMIN_EMAILS"))
    app.config.setdefault("RBAC_OPERATOR_EMAILS", _csv("RBAC_OPERATOR_EMAILS"))
    app.config.setdefault("RBAC_VIEWER_EMAILS", _csv("RBAC_VIEWER_EMAILS"))
    default_role = os.getenv("RBAC_DEFAULT_ROLE", "anonymous").strip().lower()
    app.config.setdefault(
        "RBAC_DEFAULT_ROLE",
        default_role if default_role in {"anonymous", "viewer"} else "anonymous",
    )
    app.register_blueprint(rbac_blueprint)

    @app.before_request
    def enforce_known_write_permissions():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        identity = current_identity()
        permission = None
        if request.path.startswith("/api/containers/"):
            action = request.path.rsplit("/", 1)[-1]
            if action in {"start", "stop", "restart"}:
                permission = f"container.{action}"
        elif request.path == "/api/maintenance":
            permission = "maintenance.manage"
        if permission and not has_permission(identity["role"], permission):
            return jsonify({"error": "Brugeren har ikke tilladelse til handlingen.", "required_permission": permission}), 403
        return None

    @app.after_request
    def expose_identity(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            payload["identity"] = current_identity()
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        response.headers["X-Racher-Role"] = current_identity()["role"]
        return response
