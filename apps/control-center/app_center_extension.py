from pathlib import Path

from flask import Blueprint, current_app, jsonify, make_response, render_template

from rbac_extension import current_identity
from services import module_registry_service
from services.app_registry_service import (
    build_app_center,
    load_app_registry,
    resolve_registry_links,
)
from services.docker_service import docker_status
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


def app_center_report():
    containers, docker_error = docker_status(include_usage=True)
    return build_app_center(
        current_app.config.get("APP_LINKS", []),
        containers,
        registry_errors=current_app.config.get("APP_REGISTRY_ERRORS", []),
        docker_error=docker_error,
    )


def _can_read_app_center(identity):
    return has_permission(identity["role"], "system.read")


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

    response = make_response(
        render_template(
            "app_center.html",
            report=app_center_report(),
            actor=identity.get("email") or identity["role"],
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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

    response = jsonify(
        {
            "app_center": app_center_report(),
            "actor": identity.get("email") or identity["role"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
    _register_navigation_module()
    app.register_blueprint(app_center_blueprint)
