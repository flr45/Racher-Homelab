from flask import Flask

import docker_center_extension
from docker_center_extension import init_docker_center
from rbac_extension import init_rbac


ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}


def make_app(monkeypatch, *, cleanup=False):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        ALLOWED_EMAILS={"admin@example.com"},
        PROTECTED_CONTAINERS={"control-center"},
        DOCKER_CLEANUP_ENABLED=cleanup,
    )
    init_rbac(app)
    init_docker_center(app)
    return app


def test_inventory_is_read_only_and_exposes_safety(monkeypatch):
    monkeypatch.setattr(
        docker_center_extension,
        "docker_center_inventory",
        lambda: ({"containers": [], "networks": [], "volumes": [], "images": []}, None),
    )
    app = make_app(monkeypatch)
    response = app.test_client().get("/api/docker-center", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["actions"]["enabled"] is False
    assert payload["actions"]["can_cleanup"] is True
    assert payload["actions"]["mode"] == "dangling-images-only"


def test_container_details_hide_backend_errors(monkeypatch):
    monkeypatch.setattr(docker_center_extension, "inspect_container", lambda _name: (None, "not_found"))
    app = make_app(monkeypatch)
    response = app.test_client().get("/api/docker-center/containers/missing")
    assert response.status_code == 404


def test_cleanup_requires_admin_enabled_flag_and_confirmation(monkeypatch):
    monkeypatch.setattr(
        docker_center_extension,
        "prune_dangling_images",
        lambda: ({"deleted": 2, "space_reclaimed": 1234}, None),
    )
    disabled = make_app(monkeypatch)
    assert (
        disabled.test_client().post(
            "/api/docker-center/prune-images",
            headers=ADMIN_HEADERS,
            json={"confirm": "PRUNE DANGLING IMAGES"},
        ).status_code
        == 503
    )

    enabled = make_app(monkeypatch, cleanup=True)
    client = enabled.test_client()
    assert client.post("/api/docker-center/prune-images", json={"confirm": "PRUNE DANGLING IMAGES"}).status_code == 403
    assert client.post(
        "/api/docker-center/prune-images",
        headers=ADMIN_HEADERS,
        json={"confirm": "wrong"},
    ).status_code == 400
    response = client.post(
        "/api/docker-center/prune-images",
        headers=ADMIN_HEADERS,
        json={"confirm": "PRUNE DANGLING IMAGES"},
    )
    assert response.status_code == 200
    assert response.get_json()["result"]["deleted"] == 2
