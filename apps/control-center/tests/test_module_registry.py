from flask import Flask

from module_registry_extension import init_module_registry
from rbac_extension import init_rbac

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}
ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}


def make_app():
    app = Flask(__name__, template_folder="../templates")
    app.config.update(
        TESTING=True,
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    init_rbac(app)
    init_module_registry(app)
    return app


def test_anonymous_sees_no_modules_and_no_store():
    response = make_app().test_client().get("/api/modules")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["role"] == "anonymous"
    assert payload["modules"] == []


def test_viewer_only_sees_readable_modules():
    response = make_app().test_client().get("/api/modules", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    ids = {item["id"] for item in response.get_json()["modules"]}
    assert "dashboard" in ids
    assert "docker" in ids
    assert "readiness" in ids
    assert "hardware" in ids
    assert "ssh" not in ids
    assert "deployments" not in ids


def test_admin_sees_all_modules_and_group_order():
    response = make_app().test_client().get("/api/modules", headers=ADMIN_HEADERS)
    payload = response.get_json()
    ids = {item["id"] for item in payload["modules"]}
    assert "ssh" in ids
    assert "deployments" in ids
    assert "readiness" in ids
    assert "hardware" in ids
    assert payload["groups"][0]["category"] == "overview"
    assert len(payload["modules"]) == 14


def test_control_center_renders_mobile_friendly_module_cards():
    response = make_app().test_client().get("/control", headers=ADMIN_HEADERS)
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Unified Control Center" in text
    assert "SSH Console" in text
    assert "Node &amp; Hardware Center" in text
    assert "@media(max-width:760px)" in text
