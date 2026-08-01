from flask import Flask

from module_registry_extension import init_module_registry
from rbac_extension import init_rbac
from services import module_registry_service
from unified_ui_extension import init_unified_ui

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__, template_folder="../templates")
    app.config.update(
        TESTING=True,
        SECRET_KEY="test",
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    app.add_url_rule(
        "/",
        endpoint="control_center.index",
        view_func=lambda: "deployment inventory",
    )
    init_rbac(app)
    init_unified_ui(app)
    init_module_registry(app)
    return app


def test_root_redirects_to_unified_control_center():
    response = make_app().test_client().get("/", headers=VIEWER_HEADERS)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/control")


def test_deployment_inventory_remains_available_under_module_url():
    response = make_app().test_client().get(
        "/deployment-inventory", headers=VIEWER_HEADERS
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "deployment inventory"


def test_dashboard_module_is_promoted_to_deployment_inventory():
    make_app()
    dashboard = next(
        item for item in module_registry_service.MODULES if item["id"] == "dashboard"
    )
    assert dashboard["name"] == "Deployment Inventory"
    assert dashboard["href"] == "/deployment-inventory"


def test_control_center_contains_core_navigation():
    response = make_app().test_client().get("/control", headers=VIEWER_HEADERS)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Deployment Inventory" in body
    assert "Driftsstatus" in body
    assert "App Center" in body
    assert "/deployment-inventory" in body
    assert "/operations-status" in body
    assert "/apps" in body
