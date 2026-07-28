import importlib
import os
import sys
from pathlib import Path

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]


def load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("RACHER_OS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("RACHER_OS_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_ACTIONS_ENABLED", "false")
    monkeypatch.syspath_prepend(str(CONTROL_CENTER_ROOT))

    sys.modules.pop("config", None)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    flask_app = module.create_app({"TESTING": True})
    return module, flask_app


def test_health_endpoint(monkeypatch, tmp_path):
    _, flask_app = load_app(monkeypatch, tmp_path)

    response = flask_app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_admin_actions_are_disabled_by_default(monkeypatch, tmp_path):
    module, flask_app = load_app(monkeypatch, tmp_path)

    with flask_app.test_request_context("/"):
        assert module.admin_allowed() is False


def test_database_schema_is_created(monkeypatch, tmp_path):
    module, flask_app = load_app(monkeypatch, tmp_path)

    with flask_app.app_context(), module.database() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {"metrics", "audit_log", "events"}.issubset(tables)


def test_database_enables_sqlite_hardening_and_indexes(monkeypatch, tmp_path):
    module, flask_app = load_app(monkeypatch, tmp_path)

    with flask_app.app_context(), module.database() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert journal_mode == "wal"
    assert busy_timeout == 5_000
    assert foreign_keys == 1
    assert {
        "idx_audit_recorded_at",
        "idx_events_key_id",
        "idx_events_recorded_at",
    }.issubset(indexes)


def test_create_app_applies_config_overrides(monkeypatch, tmp_path):
    _, flask_app = load_app(monkeypatch, tmp_path)

    assert flask_app.config["TESTING"] is True
    assert flask_app.config["SECRET_KEY"] == "test-secret-key"


def test_backup_service_lists_newest_first(monkeypatch, tmp_path):
    _, flask_app = load_app(monkeypatch, tmp_path)
    backup_root = flask_app.config["BACKUP_ROOT"]
    older = backup_root / "older"
    newer = backup_root / "newer"
    older.mkdir(parents=True)
    newer.mkdir()
    (older / "data.bin").write_bytes(b"old")
    (newer / "data.bin").write_bytes(b"newer")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    from services.backup_service import backups

    with flask_app.app_context():
        result = backups()

    assert [item["name"] for item in result] == ["newer", "older"]


def test_docker_service_returns_error_when_client_is_unavailable(
    monkeypatch, tmp_path
):
    _, flask_app = load_app(monkeypatch, tmp_path)
    from services import docker_service

    def unavailable_client():
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(docker_service, "docker_client", unavailable_client)

    with flask_app.app_context():
        containers, error = docker_service.docker_status()

    assert containers == []
    assert error == "docker unavailable"


def test_metrics_service_records_and_reads_history(monkeypatch, tmp_path):
    module, flask_app = load_app(monkeypatch, tmp_path)
    from services.metrics_service import metric_history, record_metrics

    metrics = {
        "cpu": 10.0,
        "ram": 20.0,
        "disk": 30.0,
        "temperature": 40.0,
        "network_sent_mb": 50.0,
        "network_recv_mb": 60.0,
    }

    with flask_app.app_context():
        record_metrics(metrics, module.database)
        points = metric_history(24, module.database)

    assert len(points) == 1
    assert points[0]["cpu"] == 10.0
    assert points[0]["network_recv_mb"] == 60.0


def test_audit_service_records_actor_and_truncates_message(monkeypatch, tmp_path):
    module, flask_app = load_app(monkeypatch, tmp_path)

    with flask_app.test_request_context(
        "/",
        headers={"Cf-Access-Authenticated-User-Email": "Admin@Example.com"},
    ):
        module.write_audit("restart", "api", True, "x" * 600)

    with flask_app.app_context():
        entries = module.audit_history()

    assert len(entries) == 1
    assert entries[0]["actor"] == "admin@example.com"
    assert entries[0]["action"] == "restart"
    assert entries[0]["target"] == "api"
    assert entries[0]["success"] == 1
    assert len(entries[0]["message"]) == 500


def test_event_service_deduplicates_same_key(monkeypatch, tmp_path):
    module, flask_app = load_app(monkeypatch, tmp_path)

    with flask_app.app_context():
        first_created = module.write_event(
            "metric:cpu", "warning", "Høj CPU", "CPU er høj."
        )
        duplicate_created = module.write_event(
            "metric:cpu", "warning", "Høj CPU", "CPU er stadig høj."
        )
        events = module.event_history()

    assert first_created is True
    assert duplicate_created is False
    assert len(events) == 1
    assert events[0]["event_key"] == "metric:cpu"
    assert events[0]["message"] == "CPU er høj."


def test_container_deployment_persists_writable_data_directory():
    repository_root = CONTROL_CENTER_ROOT.parents[1]
    compose = (repository_root / "compose/control-center/compose.yml").read_text()
    dockerfile = (CONTROL_CENTER_ROOT / "Dockerfile").read_text()

    assert "RACHER_OS_DATA: /data" in compose
    assert "- control-center-data:/data" in compose
    assert "\nvolumes:\n  control-center-data:" in compose
    assert "mkdir -p /data" in dockerfile
    assert "chown app:app /data" in dockerfile
