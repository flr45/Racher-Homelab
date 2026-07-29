from flask import Blueprint, jsonify, render_template

from rbac_extension import current_identity
from services.module_registry_service import grouped_modules, visible_modules
from services.rbac_service import has_permission

module_registry_blueprint = Blueprint("module_registry", __name__)


@module_registry_blueprint.get("/api/modules")
def api_modules():
    identity = current_identity()
    modules = visible_modules(identity["role"], has_permission)
    response = jsonify(
        {
            "role": identity["role"],
            "actor": identity.get("email") or identity["role"],
            "modules": modules,
            "groups": grouped_modules(modules),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@module_registry_blueprint.get("/control")
def control_center():
    identity = current_identity()
    modules = visible_modules(identity["role"], has_permission)
    return render_template(
        "control_center.html",
        identity=identity,
        groups=grouped_modules(modules),
        module_count=len(modules),
    )


def init_module_registry(app):
    app.register_blueprint(module_registry_blueprint)
