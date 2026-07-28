import os

from flask import Blueprint, current_app, jsonify, request

from rbac_extension import current_identity
from services.database_service import open_database
from services.docker_service import (
    cleanup_rollout_backups,
    inspect_rollout_container,
    pull_rollout_image,
    replace_rollout_container,
)
from services.rbac_service import has_permission
from services.rollout_service import (
    RolloutError,
    create_rollout,
    execute_rollout,
    list_rollouts,
)


deployment_ui_blueprint = Blueprint("deployment_ui", __name__)


def _database_factory():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def _require_permission(permission):
    identity = current_identity()
    if not has_permission(identity["role"], permission):
        return None, (
            jsonify(
                {
                    "error": "Brugeren har ikke tilladelse til handlingen.",
                    "required_permission": permission,
                }
            ),
            403,
        )
    return identity, None


@deployment_ui_blueprint.get("/api/deployment-actions")
def deployment_actions_status():
    identity = current_identity()
    return jsonify(
        {
            "enabled": bool(current_app.config.get("DEPLOYMENT_ACTIONS_ENABLED", False)),
            "can_deploy": has_permission(identity["role"], "deployment.manage"),
            "rollouts": list_rollouts(50, _database_factory),
            "safety": {
                "requires_confirmation": True,
                "automatic_rollback": True,
                "latest_tag_allowed": False,
                "protected_containers": sorted(
                    current_app.config.get("PROTECTED_CONTAINERS", ())
                ),
            },
        }
    )


@deployment_ui_blueprint.post("/api/deployment-actions/rollout")
def create_and_execute_rollout():
    identity, denied = _require_permission("deployment.manage")
    if denied:
        return denied
    if not current_app.config.get("DEPLOYMENT_ACTIONS_ENABLED", False):
        return jsonify({"error": "Deployment-handlinger er deaktiveret."}), 503

    payload = request.get_json(silent=True) or {}
    container_name = str(payload.get("container_name") or "").strip()
    target_image = str(payload.get("target_image") or "").strip()
    confirmation = str(payload.get("confirm") or "").strip()

    if not container_name or confirmation != container_name:
        return jsonify({"error": "Bekræftelsen skal matche containernavnet."}), 400
    if container_name in current_app.config.get("PROTECTED_CONTAINERS", ()):
        return jsonify({"error": "Containeren er beskyttet mod deployment."}), 403

    try:
        rollout = create_rollout(
            container_name,
            target_image,
            identity.get("email") or identity["role"],
            _database_factory,
        )
        completed = execute_rollout(
            rollout["id"],
            _database_factory,
            pull_image=pull_rollout_image,
            replace_container=replace_rollout_container,
            inspect_container=inspect_rollout_container,
            cleanup_backups=cleanup_rollout_backups,
            timeout_seconds=current_app.config.get(
                "DEPLOYMENT_HEALTH_TIMEOUT_SECONDS", 120
            ),
        )
    except RolloutError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("deployment_ui_unexpected_error")
        return jsonify({"error": "Deployment kunne ikke gennemføres."}), 500

    status_code = 200 if completed["status"] == "succeeded" else 409
    return jsonify({"rollout": completed}), status_code


def init_deployment_ui(app):
    app.config.setdefault(
        "DEPLOYMENT_ACTIONS_ENABLED",
        os.getenv("DEPLOYMENT_ACTIONS_ENABLED", "false").lower() == "true",
    )
    app.config.setdefault(
        "DEPLOYMENT_HEALTH_TIMEOUT_SECONDS",
        min(600, max(15, int(os.getenv("DEPLOYMENT_HEALTH_TIMEOUT_SECONDS", "120")))),
    )
    app.register_blueprint(deployment_ui_blueprint)
