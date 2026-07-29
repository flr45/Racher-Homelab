import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app_center_extension import init_app_center
from rbac_extension import init_rbac
from services.app_registry_service import build_app_center, load_app_registry

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def write_manifest(root, **overrides):
    manifest = {
        "id": "minutregnskab",
        "name": "Minutregnskab",
        "description": "Minutregnskab til ambulancevagter.",
        "icon": "timer",
        "category": "apps",
        "service": "minutregnskab",
        "image": "ghcr.io/flr45/minutregnskab:latest",
        "compose_file": "compose/minutregnskab/compose.yml",
        "url_env": "MINUTREGNSKAB_URL",
        "url_default": "#",
        "backup": False,
        "auto_update": True,
    }
    manifest.update(overrides)
    (root / f"{manifest['id']}.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def make_app(tmp_path):
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    write_manifest(registry_root)

    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        APP_REGISTRY_ROOT=registry_root,
        APP_LINKS=[],
        PROTECTED_CONTAINERS=set(),
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    init_rbac(app)
    init_app_center(app)
    return app


def test_registry_rejects_invalid_and_duplicate_manifests(tmp_path):
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    write_manifest(registry_root)
    write_manifest(registry_root, id="duplicate", service="minutregnskab")
    (registry_root / "broken.json").write_text("{", encoding="utf-8")

    apps, errors = load_app_registry(registry_root)

    assert [item["id"] for item in apps] == ["minutregnskab"]
    assert len(errors) == 2
    assert any("dublet service" in error for error in errors)


def test_app_center_matches_container_and_exposes_live_metadata():
    apps = [
        {
            "id": "minutregnskab",
            "name": "Minutregnskab",
            "service": "minutregnskab",
            "image": "ghcr.io/flr45/minutregnskab:latest",
            "url": "#",
        }
    ]
    containers = [
        {
            "name": "minutregnskab",
            "status": "running",
            "healthy": "healthy",
            "image": "ghcr.io/flr45/minutregnskab:latest",
            "cpu": 1.2,
            "memory_mb": 42.5,
            "container_id": "abc123",
            "compose_project": "minutregnskab",
            "compose_service": "minutregnskab",
            "started_at": "2026-07-29T12:00:00+00:00",
        }
    ]

    report = build_app_center(apps, containers)
    item = report["apps"][0]

    assert report["summary"]["running"] == 1
    assert report["summary"]["missing"] == 0
    assert item["installed"] is True
    assert item["health"] == "healthy"
    assert item["version"] == "latest"
    assert item["cpu"] == 1.2
    assert item["memory_mb"] == 42.5


def test_app_center_api_requires_permission(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "app_center_extension.docker_status", lambda include_usage=True: ([], None)
    )

    response = app.test_client().get("/api/app-center")

    assert response.status_code == 403


def test_app_center_api_uses_registry_and_docker_discovery(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    containers = [
        {
            "name": "minutregnskab",
            "status": "running",
            "healthy": "healthy",
            "image": "ghcr.io/flr45/minutregnskab:latest",
            "cpu": 0.4,
            "memory_mb": 31.0,
            "container_id": "container-id",
            "compose_project": "minutregnskab",
            "compose_service": "minutregnskab",
            "started_at": None,
        }
    ]
    monkeypatch.setattr(
        "app_center_extension.docker_status",
        lambda include_usage=True: (containers, None),
    )

    response = app.test_client().get("/api/app-center", headers=VIEWER_HEADERS)
    report = response.get_json()["app_center"]

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert report["read_only"] is True
    assert report["summary"]["registered"] == 1
    assert report["summary"]["running"] == 1
    assert report["apps"][0]["name"] == "Minutregnskab"
    assert report["apps"][0]["health"] == "healthy"
