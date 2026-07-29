from flask import Blueprint, current_app, jsonify

from rbac_extension import current_identity
from services.configuration_drift_service import build_configuration_drift_report
from services.rbac_service import has_permission

configuration_drift_blueprint = Blueprint("configuration_drift", __name__)


@configuration_drift_blueprint.get("/api/configuration-drift")
def api_configuration_drift():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke adgang til konfigurationsdrift.",
                "required_permission": "system.read",
            }
        ), 403

    response = jsonify(
        {
            "report": build_configuration_drift_report(current_app.config),
            "actor": identity.get("email") or identity["role"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def init_configuration_drift_center(app):
    app.register_blueprint(configuration_drift_blueprint)
