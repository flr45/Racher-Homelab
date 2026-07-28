import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from rbac_extension import init_rbac
from services.rbac_service import has_permission, resolve_role


def test_role_resolution_and_permissions():
    assert resolve_role("admin@example.test", admins={"admin@example.test"}) == "admin"
    assert resolve_role("operator@example.test", operators={"operator@example.test"}) == "operator"
    assert resolve_role("viewer@example.test", viewers={"viewer@example.test"}) == "viewer"
    assert resolve_role("unknown@example.test", default_role="anonymous") == "anonymous"
    assert has_permission("admin", "maintenance.manage")
    assert has_permission("operator", "container.restart")
    assert not has_permission("operator", "container.stop")
    assert has_permission("viewer", "system.read")


def test_identity_api_and_status(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATA_ROOT": tmp_path,
        "DATABASE_PATH": tmp_path / "test.db",
        "RBAC_ADMIN_EMAILS": {"admin@example.test"},
        "RBAC_OPERATOR_EMAILS": {"operator@example.test"},
        "RBAC_VIEWER_EMAILS": {"viewer@example.test"},
        "RBAC_DEFAULT_ROLE": "anonymous",
    })
    init_rbac(app)
    client = app.test_client()

    response = client.get(
        "/api/identity",
        headers={"Cf-Access-Authenticated-User-Email": "Operator@Example.Test"},
    )
    assert response.status_code == 200
    identity = response.get_json()["identity"]
    assert identity["email"] == "operator@example.test"
    assert identity["role"] == "operator"
    assert "container.restart" in identity["permissions"]
    assert response.headers["X-Racher-Role"] == "operator"

    status = client.get(
        "/api/status",
        headers={"Cf-Access-Authenticated-User-Email": "viewer@example.test"},
    )
    assert status.status_code == 200
    assert status.get_json()["identity"]["role"] == "viewer"


def test_known_write_permissions_are_denied_before_route(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATA_ROOT": tmp_path,
        "DATABASE_PATH": tmp_path / "test.db",
        "RBAC_ADMIN_EMAILS": {"admin@example.test"},
        "RBAC_OPERATOR_EMAILS": {"operator@example.test"},
        "RBAC_VIEWER_EMAILS": set(),
        "RBAC_DEFAULT_ROLE": "anonymous",
    })
    init_rbac(app)
    client = app.test_client()

    denied = client.post(
        "/api/containers/demo/stop",
        headers={"Cf-Access-Authenticated-User-Email": "operator@example.test"},
    )
    assert denied.status_code == 403
    assert denied.get_json()["required_permission"] == "container.stop"

    anonymous = client.post("/api/maintenance", json={})
    assert anonymous.status_code == 403
    assert anonymous.get_json()["required_permission"] == "maintenance.manage"
