from flask import Blueprint, current_app, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission
from services.readiness_service import build_readiness_report

readiness_blueprint = Blueprint("readiness", __name__)


@readiness_blueprint.get("/api/readiness")
def api_readiness():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke tilladelse til readinessrapporten.",
                "required_permission": "system.read",
            }
        ), 403

    report = build_readiness_report(current_app.config)
    response = jsonify(
        {
            "report": report,
            "actor": identity.get("email") or identity["role"],
            "read_only": True,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def init_readiness_center(app):
    app.register_blueprint(readiness_blueprint)
