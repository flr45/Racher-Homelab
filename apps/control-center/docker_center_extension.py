# ruff: noqa: I001
import os

from flask import Blueprint, current_app, jsonify, request

from rbac_extension import current_identity
from services.docker_center_service import (
    docker_center_inventory,
    inspect_container,
    prune_dangling_images,
)
from services.rbac_service import has_permission


docker_center_blueprint = Blueprint("docker_center_extended", __name__)


@docker_center_blueprint.get("/api/docker-center")
def docker_center_status():
    inventory, error = docker_center_inventory()
    if error:
        return jsonify({"error": "Docker-status kunne ikke hentes."}), 503
    identity = current_identity()
    return jsonify(
        {
            "inventory": inventory,
            "actions": {
                "enabled": bool(current_app.config.get("DOCKER_CLEANUP_ENABLED", False)),
                "can_cleanup": has_permission(identity["role"], "docker.manage"),
                "mode": "dangling-images-only",
                "requires_confirmation": True,
            },
        }
    )


@docker_center_blueprint.get("/api/docker-center/containers/<container_name>")
def docker_container_details(container_name):
    details, error = inspect_container(container_name)
    if error == "not_found":
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    if error:
        return jsonify({"error": "Containerdetaljer kunne ikke hentes."}), 503
    return jsonify({"container": details})


@docker_center_blueprint.post("/api/docker-center/prune-images")
def prune_images():
    identity = current_identity()
    if not has_permission(identity["role"], "docker.manage"):
        return jsonify(
            {
                "error": "Brugeren har ikke tilladelse til handlingen.",
                "required_permission": "docker.manage",
            }
        ), 403
    if not current_app.config.get("DOCKER_CLEANUP_ENABLED", False):
        return jsonify({"error": "Docker-oprydning er deaktiveret."}), 503
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "PRUNE DANGLING IMAGES":
        return jsonify({"error": "Bekræftelsen er ugyldig."}), 400
    result, error = prune_dangling_images()
    if error:
        current_app.logger.error("docker_image_prune_failed")
        return jsonify({"error": "Docker-oprydning kunne ikke gennemføres."}), 500
    current_app.logger.info(
        "docker_images_pruned",
        extra={
            "actor": identity.get("email") or identity["role"],
            "reclaimed_bytes": result["space_reclaimed"],
        },
    )
    return jsonify({"result": result})


def init_docker_center(app):
    app.config.setdefault(
        "DOCKER_CLEANUP_ENABLED",
        os.getenv("DOCKER_CLEANUP_ENABLED", "false").lower() == "true",
    )
    app.register_blueprint(docker_center_blueprint)
