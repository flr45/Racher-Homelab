from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.node_hardware_service import node_inventory
from services.rbac_service import has_permission

node_hardware_blueprint = Blueprint("node_hardware", __name__)


@node_hardware_blueprint.get("/api/node-hardware")
def api_node_hardware():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke tilladelse til Node & Hardware Center.",
                "required_permission": "system.read",
            }
        ), 403
    response = jsonify(
        {
            "node": node_inventory(),
            "actor": identity.get("email") or identity["role"],
            "read_only": True,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def init_node_hardware_center(app):
    app.register_blueprint(node_hardware_blueprint)
