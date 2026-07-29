from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission
from services.release_service import build_release_payload

release_blueprint = Blueprint("release_center", __name__)


@release_blueprint.get("/api/release")
def release_status():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify({"error": "Brugeren har ikke adgang til Release Center."}), 403
    payload = build_release_payload()
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def init_release_center(app):
    app.register_blueprint(release_blueprint)
