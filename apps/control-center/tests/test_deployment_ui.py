from pathlib import Path

import deployment_ui_extension
from deployment_ui_extension import init_deployment_ui
from flask import Flask
from rbac_extension import init_rbac


ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}


def make_app(tmp_path, monkeypatch, *, enabled=False):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        DATA_ROOT=Path(tmp_path),
        DATABASE_PATH=Path(tmp_path) / "racher-os.db",
        PROTECTED_CONTAINERS={"control-center"},
        ALLOWED_EMAILS={"admin@example.com"},
        DEPLOYMENT_ACTIONS_ENABLED=enabled,
        DEPLOYMENT_HEALTH_TIMEOUT_SECONDS=30,
    )
    monkeypatch.setattr(deployment_ui_extension, "list_rollouts", lambda *_: [])
    init_rbac(app)
    init_deployment_ui(app)
    return app


def test_status_is_read_only_and_exposes_safety(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    response = app.test_client().get("/api/deployment-actions", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["enabled"] is False
    assert payload["can_deploy"] is True
    assert payload["safety"]["requires_confirmation"] is True
    assert "control-center" in payload["safety"]["protected_containers"]


def test_anonymous_cannot_create_rollout(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, enabled=True)
    response = app.test_client().post(
        "/api/deployment-actions/rollout",
        json={"container_name": "app", "target_image": "repo/app:1.2.3", "confirm": "app"},
    )
    assert response.status_code == 403
    assert response.get_json()["required_permission"] == "deployment.manage"


def test_disabled_actions_fail_closed(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    response = app.test_client().post(
        "/api/deployment-actions/rollout",
        headers=ADMIN_HEADERS,
        json={"container_name": "app", "target_image": "repo/app:1.2.3", "confirm": "app"},
    )
    assert response.status_code == 503


def test_confirmation_and_protected_container_are_enforced(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, enabled=True)
    client = app.test_client()
    mismatch = client.post(
        "/api/deployment-actions/rollout",
        headers=ADMIN_HEADERS,
        json={"container_name": "app", "target_image": "repo/app:1.2.3", "confirm": "wrong"},
    )
    protected = client.post(
        "/api/deployment-actions/rollout",
        headers=ADMIN_HEADERS,
        json={
            "container_name": "control-center",
            "target_image": "repo/app:1.2.3",
            "confirm": "control-center",
        },
    )
    assert mismatch.status_code == 400
    assert protected.status_code == 403


def test_successful_rollout_uses_existing_safe_engine(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, enabled=True)
    seen = {}

    def fake_create(container_name, target_image, actor, database_factory):
        seen.update(container=container_name, image=target_image, actor=actor)
        return {"id": 7}

    def fake_execute(rollout_id, database_factory, **kwargs):
        seen["rollout_id"] = rollout_id
        seen["timeout"] = kwargs["timeout_seconds"]
        return {"id": rollout_id, "status": "succeeded", "phase": "complete"}

    monkeypatch.setattr(deployment_ui_extension, "create_rollout", fake_create)
    monkeypatch.setattr(deployment_ui_extension, "execute_rollout", fake_execute)

    response = app.test_client().post(
        "/api/deployment-actions/rollout",
        headers=ADMIN_HEADERS,
        json={"container_name": "app", "target_image": "repo/app:1.2.3", "confirm": "app"},
    )
    assert response.status_code == 200
    assert response.get_json()["rollout"]["status"] == "succeeded"
    assert seen == {
        "container": "app",
        "image": "repo/app:1.2.3",
        "actor": "admin@example.com",
        "rollout_id": 7,
        "timeout": 30,
    }
