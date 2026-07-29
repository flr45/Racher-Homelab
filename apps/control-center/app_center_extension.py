from pathlib import Path

from flask import Blueprint, current_app, jsonify

from rbac_extension import current_identity
from services.app_registry_service import (
    build_app_center,
    load_app_registry,
    resolve_registry_links,
)
from services.docker_service import docker_status
from services.rbac_service import has_permission

app_center_blueprint = Blueprint("app_center", __name__)


def app_center_report():
    containers, docker_error = docker_status(include_usage=True)
    return build_app_center(
        current_app.config.get("APP_LINKS", []),
        containers,
        registry_errors=current_app.config.get("APP_REGISTRY_ERRORS", []),
        docker_error=docker_error,
    )


@app_center_blueprint.get("/api/app-center")
def api_app_center():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke adgang til App Center.",
                "required_permission": "system.read",
            }
        ), 403

    response = jsonify(
        {
            "app_center": app_center_report(),
            "actor": identity.get("email") or identity["role"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
    app.register_blueprint(app_center_blueprint)
