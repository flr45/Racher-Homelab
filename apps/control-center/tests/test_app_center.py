import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app_center_extension import init_app_center
from rbac_extension import init_rbac
from services import module_registry_service
from services.app_registry_service import build_app_center, load_app_registry

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}
OPERATOR_HEADERS = {"Cf-Access-Authenticated-User-Email": "operator@example.com"}
ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}


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


def make_app(
    tmp_path,
    *,
    actions_enabled=False,
    manifest_overrides=None,
    protected_containers=None,
):
    registry_root = tmp_path / "registry"
    registry_root.mkdir(parents=True)
    write_manifest(registry_root, **(manifest_overrides or {}))

    template_root = Path(__file__).resolve().parents[1] / "templates"
    app = Flask(__name__, template_folder=str(template_root))
    app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        APP_REGISTRY_ROOT=registry_root,
        APP_LINKS=[],
        APP_ACTIONS_ENABLED=actions_enabled,
        PROTECTED_CONTAINERS=set(protected_containers or ()),
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_OPERATOR_EMAILS={"operator@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
        DATA_ROOT=tmp_path / "data",
        DATABASE_PATH=tmp_path / "data" / "racher-os.db",
    )
    init_rbac(app)
    init_app_center(app)
    return app


def installed_container():
    return {
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


def post_action(client, action, headers, *, confirmation=None, csrf="test-csrf"):
    if csrf is not None:
        with client.session_transaction() as session:
            session["csrf_token"] = csrf
    request_headers = dict(headers)
    if csrf is not None:
        request_headers["X-CSRF-Token"] = csrf
    return client.post(
        f"/api/app-center/minutregnskab/{action}",
        headers=request_headers,
        json={"confirm": confirmation or f"{action.upper()} minutregnskab"},
    )


def test_registry_rejects_invalid_and_duplicate_manifests(tmp_path):
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    write_manifest(registry_root)
    write_manifest(registry_root, id="z-duplicate", service="minutregnskab")
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
    report = build_app_center(apps, [installed_container()])
    item = report["apps"][0]

    assert report["summary"]["running"] == 1
    assert report["summary"]["missing"] == 0
    assert item["installed"] is True
    assert item["health"] == "healthy"
    assert item["version"] == "latest"
    assert item["cpu"] == 0.4
    assert item["memory_mb"] == 31.0


def test_app_center_api_requires_permission(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "app_center_extension.docker_status", lambda include_usage=True: ([], None)
    )

    response = app.test_client().get("/api/app-center")

    assert response.status_code == 403


def test_app_center_api_uses_registry_and_docker_discovery(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "app_center_extension.docker_status",
        lambda include_usage=True: ([installed_container()], None),
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
    assert report["apps"][0]["allowed_actions"] == []


def test_app_center_page_renders_live_cards(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "app_center_extension.docker_status", lambda include_usage=True: ([], None)
    )

    response = app.test_client().get("/apps", headers=VIEWER_HEADERS)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert b"App Center" in response.data
    assert b"Minutregnskab" in response.data
    assert b"Ikke installeret" in response.data
    assert b"Sikker l\xc3\xa6setilstand" in response.data


def test_app_center_registers_control_navigation_once(tmp_path):
    make_app(tmp_path)
    ids = [item["id"] for item in module_registry_service.MODULES]

    assert ids.count("app-center") == 1


def test_actions_fail_closed_when_disabled(tmp_path):
    app = make_app(tmp_path, actions_enabled=False)

    response = post_action(app.test_client(), "restart", ADMIN_HEADERS)

    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["error"] == "App-handlinger er deaktiveret."


def test_operator_can_restart_registered_application(tmp_path, monkeypatch):
    app = make_app(tmp_path, actions_enabled=True)
    calls = []
    audits = []
    monkeypatch.setattr(
        "app_center_extension.perform_container_action",
        lambda service, action: calls.append((service, action)),
    )
    monkeypatch.setattr(
        "app_center_extension._write_audit",
        lambda *args: audits.append(args),
    )

    response = post_action(app.test_client(), "restart", OPERATOR_HEADERS)

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [("minutregnskab", "restart")]
    assert audits[0][0:4] == (
        "restart",
        "minutregnskab",
        True,
        "Udført på minutregnskab",
    )


def test_operator_cannot_stop_or_manage_infrastructure(tmp_path):
    app = make_app(tmp_path, actions_enabled=True)
    stop_response = post_action(app.test_client(), "stop", OPERATOR_HEADERS)

    assert stop_response.status_code == 403
    assert stop_response.get_json()["required_permission"] == "container.stop"

    infra_app = make_app(
        tmp_path / "infra",
        actions_enabled=True,
        manifest_overrides={"category": "infrastructure"},
    )
    infra_response = post_action(
        infra_app.test_client(), "restart", OPERATOR_HEADERS
    )

    assert infra_response.status_code == 403


def test_action_requires_csrf_and_exact_confirmation(tmp_path):
    app = make_app(tmp_path, actions_enabled=True)
    client = app.test_client()

    no_csrf = post_action(client, "restart", ADMIN_HEADERS, csrf=None)
    wrong_confirmation = post_action(
        client,
        "restart",
        ADMIN_HEADERS,
        confirmation="RESTART something-else",
    )

    assert no_csrf.status_code == 403
    assert wrong_confirmation.status_code == 400
    assert wrong_confirmation.get_json()["required_confirmation"] == (
        "RESTART minutregnskab"
    )


def test_action_rejects_unknown_app_and_protected_container(tmp_path, monkeypatch):
    app = make_app(
        tmp_path,
        actions_enabled=True,
        protected_containers={"minutregnskab"},
    )
    monkeypatch.setattr("app_center_extension._write_audit", lambda *args: None)
    client = app.test_client()

    with client.session_transaction() as session:
        session["csrf_token"] = "test-csrf"
    unknown = client.post(
        "/api/app-center/unknown/restart",
        headers={**ADMIN_HEADERS, "X-CSRF-Token": "test-csrf"},
        json={"confirm": "RESTART unknown"},
    )
    protected = post_action(client, "restart", ADMIN_HEADERS)

    assert unknown.status_code == 404
    assert protected.status_code == 409
    assert protected.get_json()["error"] == "Containeren er beskyttet."
