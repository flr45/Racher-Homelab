import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_CENTER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_CENTER_ROOT))

from services.database_service import open_database
from services.deployment_service import (
    deployment_center_status,
    list_deployment_history,
    list_deployments,
    sync_deployment_inventory,
)


def database_factory(tmp_path):
    data_root = tmp_path / "data"
    database_path = data_root / "racher-os.db"
    return lambda: open_database(data_root, database_path)


def deployment(
    *,
    name="api",
    image="ghcr.io/example/api:1.0",
    image_id="sha256:image-v1",
    container_id="container-v1",
    status="running",
    health="healthy",
):
    return {
        "name": name,
        "container_id": container_id,
        "status": status,
        "healthy": health,
        "image": image,
        "image_id": image_id,
        "image_digest": f"ghcr.io/example/api@{image_id}",
        "created_at": "2026-01-01T00:00:00Z",
        "compose_project": "racher",
        "compose_service": name,
    }


def configured_app():
    flask_app = Flask(__name__)
    flask_app.config.update(
        CPU_WARNING=85,
        RAM_WARNING=85,
        DISK_WARNING=85,
        TEMP_WARNING=75,
        BACKUP_MAX_AGE_HOURS=36,
        NOTIFICATION_MIN_SEVERITY="critical",
    )
    return flask_app


def test_deployment_inventory_discovers_without_duplicate_history(tmp_path):
    factory = database_factory(tmp_path)
    first_seen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second_seen = first_seen + timedelta(minutes=1)

    first_changes = sync_deployment_inventory(
        [deployment()],
        factory,
        recorded_at=first_seen,
    )
    second_changes = sync_deployment_inventory(
        [deployment()],
        factory,
        recorded_at=second_seen,
    )
    current = list_deployments(factory)
    history = list_deployment_history(10, factory)

    assert [item["change_type"] for item in first_changes] == ["discovered"]
    assert second_changes == []
    assert len(current) == 1
    assert current[0]["first_seen_at"] == first_seen.isoformat()
    assert current[0]["last_seen_at"] == second_seen.isoformat()
    assert current[0]["present"] == 1
    assert [item["change_type"] for item in history] == ["discovered"]


def test_deployment_inventory_records_image_change(tmp_path):
    factory = database_factory(tmp_path)
    first_seen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    changed_at = first_seen + timedelta(hours=1)
    sync_deployment_inventory([deployment()], factory, recorded_at=first_seen)

    changes = sync_deployment_inventory(
        [
            deployment(
                image="ghcr.io/example/api:2.0",
                image_id="sha256:image-v2",
                container_id="container-v2",
            )
        ],
        factory,
        recorded_at=changed_at,
    )
    history = list_deployment_history(10, factory)
    status = deployment_center_status(factory, checked_at=changed_at)

    assert [item["change_type"] for item in changes] == ["image_changed"]
    assert history[0]["change_type"] == "image_changed"
    assert history[0]["previous_image_id"] == "sha256:image-v1"
    assert history[0]["image_id"] == "sha256:image-v2"
    assert status["total"] == 1
    assert status["present"] == 1
    assert status["running"] == 1
    assert status["changed_last_24h"] == 1
    assert status["compose_projects"] == ["racher"]


def test_deployment_inventory_tracks_missing_and_restored(tmp_path):
    factory = database_factory(tmp_path)
    first_seen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    missing_at = first_seen + timedelta(minutes=1)
    restored_at = first_seen + timedelta(minutes=2)
    sync_deployment_inventory([deployment()], factory, recorded_at=first_seen)

    missing_changes = sync_deployment_inventory([], factory, recorded_at=missing_at)
    missing_status = deployment_center_status(factory, checked_at=missing_at)
    restored_changes = sync_deployment_inventory(
        [deployment()],
        factory,
        recorded_at=restored_at,
    )
    current = list_deployments(factory)

    assert [item["change_type"] for item in missing_changes] == ["missing"]
    assert missing_status["missing"] == 1
    assert missing_status["present"] == 0
    assert [item["change_type"] for item in restored_changes] == ["restored"]
    assert current[0]["present"] == 1
    assert current[0]["missing_since"] is None


def test_docker_status_exposes_compose_and_image_metadata(monkeypatch):
    from services import docker_service

    class FakeImage:
        tags = ["ghcr.io/example/api:1.0"]
        id = "sha256:image-v1"
        short_id = "sha256:short"
        attrs = {"RepoDigests": ["ghcr.io/example/api@sha256:digest"]}

    class FakeContainer:
        name = "api"
        id = "container-v1"
        status = "running"
        labels = {
            "com.docker.compose.project": "racher",
            "com.docker.compose.service": "api",
        }
        image = FakeImage()
        attrs = {
            "Created": "2026-01-01T00:00:00Z",
            "State": {
                "StartedAt": "2026-01-01T00:00:01Z",
                "Health": {"Status": "healthy"},
            },
        }

        def stats(self, stream=False):
            assert stream is False
            return {}

    class FakeContainers:
        def list(self, all=False):
            assert all is True
            return [FakeContainer()]

    class FakeClient:
        containers = FakeContainers()

    monkeypatch.setattr(docker_service, "docker_client", FakeClient)
    flask_app = Flask(__name__)
    flask_app.config["PROTECTED_CONTAINERS"] = {"api"}

    with flask_app.app_context():
        containers, error = docker_service.docker_status()

    assert error is None
    assert containers[0]["container_id"] == "container-v1"
    assert containers[0]["image"] == "ghcr.io/example/api:1.0"
    assert containers[0]["image_id"] == "sha256:image-v1"
    assert containers[0]["image_digest"] == "ghcr.io/example/api@sha256:digest"
    assert containers[0]["compose_project"] == "racher"
    assert containers[0]["compose_service"] == "api"
    assert containers[0]["protected"] is True


def test_docker_failure_does_not_mark_inventory_missing(monkeypatch, tmp_path):
    from services import monitoring_service

    factory = database_factory(tmp_path)
    sync_deployment_inventory([deployment()], factory)
    metrics = {
        "cpu": 10.0,
        "ram": 20.0,
        "disk": 30.0,
        "temperature": None,
        "uptime": "1d 0t 0m",
        "network_sent_mb": 10.0,
        "network_recv_mb": 20.0,
    }
    monkeypatch.setattr(
        monitoring_service,
        "docker_status",
        lambda include_usage=True: ([], "docker unavailable"),
    )
    monkeypatch.setattr(monitoring_service, "system_metrics", lambda: metrics)
    monkeypatch.setattr(monitoring_service, "newest_backup", lambda: None)

    with configured_app().app_context():
        monitoring_service.collect_snapshot(factory)

    current = list_deployments(factory)
    assert current[0]["present"] == 1
    assert current[0]["missing_since"] is None


def test_deployment_api_returns_inventory_without_docker_access(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("RACHER_OS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("RACHER_OS_SECRET_KEY", "test-secret-key")
    monkeypatch.syspath_prepend(str(CONTROL_CENTER_ROOT))
    sys.modules.pop("config", None)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    flask_app = module.create_app({"TESTING": True})

    with flask_app.app_context():
        sync_deployment_inventory([deployment()], module.database)

    response = flask_app.test_client().get("/api/deployments?limit=10")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["deployment_center"]["total"] == 1
    assert payload["deployments"][0]["container_name"] == "api"
    assert payload["history"][0]["change_type"] == "discovered"


def test_deployment_schema_and_indexes_are_created(tmp_path):
    factory = database_factory(tmp_path)
    with factory() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert {"deployments", "deployment_history"}.issubset(tables)
    assert {
        "idx_deployments_present_project",
        "idx_deployment_history_recorded_at",
        "idx_deployment_history_container",
    }.issubset(indexes)
