from flask import Blueprint, current_app, redirect, request, url_for

from services import module_registry_service

unified_ui_blueprint = Blueprint("unified_ui", __name__)


@unified_ui_blueprint.get("/deployment-inventory")
def deployment_inventory():
    """Expose the existing deployment dashboard under a stable module URL."""
    view = current_app.view_functions.get("control_center.index")
    if view is None:
        return {"error": "Deployment Inventory er ikke registreret."}, 503
    return view()


def _promote_core_modules():
    modules = []
    for module in module_registry_service.MODULES:
        item = dict(module)
        if item.get("id") == "dashboard":
            item.update(
                {
                    "name": "Deployment Inventory",
                    "description": "Live serverdata, deployment-inventory, hændelser, notifikationer og Docker-drift.",
                    "href": "/deployment-inventory",
                }
            )
        modules.append(item)
    module_registry_service.MODULES = tuple(modules)


def init_unified_ui(app):
    _promote_core_modules()
    app.register_blueprint(unified_ui_blueprint)

    @app.before_request
    def unified_root():
        if request.path == "/" and request.endpoint == "control_center.index":
            return redirect(url_for("module_registry.control_center"), code=302)
        return None
