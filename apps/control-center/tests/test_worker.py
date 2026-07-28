import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_CENTER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_CENTER_ROOT))


def database_factory(tmp_path):
    from services.database_service import open_database

    data_root = tmp_path / "data"
    database_path = data_root / "racher-os.db"
    return lambda: open_database(data_root, database_path)


def configured_app():
    flask_app = Flask(__name__)
    flask_app.config.update(
        CPU_WARNING=85,
        RAM_WARNING=85,
        DISK_WARNING=85,
        TEMP_WARNING=75,
        BACKUP_MAX_AGE_HOURS=36,
        WORKER_HEALTH_MAX_AGE_SECONDS=180,
    )
    return flask_app


def test_monitoring_cycle_records_metrics_and_findings(monkeypatch, tmp_path):
    from services import monitoring_service
    from services.event_service import list_events
    from services.metrics_service import metric_history

    factory = database_factory(tmp_path)
    metrics = {
        "cpu": 90.0,
        "ram": 20.0,
        "disk": 30.0,
        "temperature": 40.0,
        "uptime": "1d 0t 0m",
        "network_sent_mb": 10.0,
        "network_recv_mb": 20.0,
    }
    monkeypatch.setattr(
        monitoring_service,
        "docker_status",
        lambda include_usage=True: (
            [
                {
                    "name": "stopped-app",
                    "status": "exited",
                    "healthy": None,
                }
            ],
            None,
        ),
    )
    monkeypatch.setattr(monitoring_service, "system_metrics", lambda: metrics)
    monkeypatch.setattr(monitoring_service, "newest_backup", lambda: None)

    with configured_app().app_context():
        snapshot = monitoring_service.collect_snapshot(factory)

    points = metric_history(24, factory)
    events = list_events(10, factory)

    assert snapshot[2] == metrics
    assert len(points) == 1
    assert {event["event_key"] for event in events} == {
        "metric:cpu",
        "container:stopped-app:stopped",
        "backup:missing",
    }


def test_monitoring_cycle_records_docker_connection_failure(monkeypatch, tmp_path):
    from services import monitoring_service
    from services.event_service import list_events

    factory = database_factory(tmp_path)
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

    events = list_events(10, factory)

    assert any(event["event_key"] == "docker:unavailable" for event in events)


def test_worker_cycle_records_successful_heartbeat(monkeypatch, tmp_path):
    import worker
    from services.worker_service import get_worker_status

    factory = database_factory(tmp_path)
    monkeypatch.setattr(
        worker,
        "collect_snapshot",
        lambda database_factory: ([], None, {"cpu": 1.0, "ram": 2.0}, None, []),
    )

    assert worker.run_cycle(configured_app(), factory) is True

    status = get_worker_status(factory, max_age_seconds=180)
    assert status["healthy"] is True
    assert status["last_success_at"] is not None
    assert status["last_error"] is None
    assert status["consecutive_failures"] == 0


def test_worker_cycle_records_failure_without_crashing(monkeypatch, tmp_path):
    import worker
    from services.worker_service import get_worker_status

    factory = database_factory(tmp_path)

    def fail_cycle(_database_factory):
        raise RuntimeError("monitoring failed")

    monkeypatch.setattr(worker, "collect_snapshot", fail_cycle)

    assert worker.run_cycle(configured_app(), factory) is False

    status = get_worker_status(factory, max_age_seconds=180)
    assert status["healthy"] is True
    assert status["last_error"] == "monitoring failed"
    assert status["consecutive_failures"] == 1


def test_worker_status_detects_stale_heartbeat(tmp_path):
    from services.worker_service import get_worker_status, record_worker_heartbeat

    factory = database_factory(tmp_path)
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record_worker_heartbeat(factory, success=True, recorded_at=recorded_at)

    status = get_worker_status(
        factory,
        max_age_seconds=180,
        checked_at=recorded_at + timedelta(seconds=181),
    )

    assert status["healthy"] is False
    assert status["status"] == "stale"
    assert status["age_seconds"] == 181


def test_worker_interval_accounts_for_cycle_runtime():
    import worker

    assert worker.remaining_interval(10.0, 60, finished_at=25.0) == 45.0
    assert worker.remaining_interval(10.0, 60, finished_at=75.0) == 0.0


def test_worker_is_deployed_with_shared_persistent_data():
    repository_root = CONTROL_CENTER_ROOT.parents[1]
    compose = (repository_root / "compose/control-center/compose.yml").read_text()

    assert "control-center-worker:" in compose
    assert 'command: ["python", "worker.py"]' in compose
    assert 'test: ["CMD", "python", "worker.py", "--healthcheck"]' in compose
    assert compose.count("- control-center-data:/data") == 2
    assert "MONITOR_INTERVAL_SECONDS: ${MONITOR_INTERVAL_SECONDS:-60}" in compose
