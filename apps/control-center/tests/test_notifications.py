import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_CENTER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_CENTER_ROOT))

from flask import Flask

from services.database_service import open_database
from services.monitoring_service import analyze_system
from services.notification_service import (
    configured_channels,
    dispatch_pending_notifications,
    enqueue_finding_notifications,
    list_notifications,
    notification_center_status,
    purge_old_notifications,
)


def database_factory(tmp_path):
    data_root = tmp_path / "data"
    database_path = data_root / "racher-os.db"
    return lambda: open_database(data_root, database_path)


def critical_finding():
    return {
        "key": "container:api:stopped",
        "severity": "critical",
        "title": "Container stoppet",
        "message": "api har status exited.",
    }


def test_configured_channels_require_valid_complete_configuration():
    assert configured_channels(
        {
            "NOTIFICATION_WEBHOOK_URL": "file:///tmp/not-allowed",
            "PUSHOVER_APP_TOKEN": "token-only",
        }
    ) == {}

    channels = configured_channels(
        {
            "NOTIFICATION_WEBHOOK_URL": "https://example.test/webhook-secret",
            "PUSHOVER_APP_TOKEN": "app-token",
            "PUSHOVER_USER_KEY": "user-key",
        }
    )

    assert sorted(channels) == ["pushover", "webhook"]


def test_enqueue_respects_minimum_severity_and_dispatches_successfully(tmp_path):
    factory = database_factory(tmp_path)
    channels = {"webhook": {"kind": "webhook", "url": "https://example.test/hook"}}
    timestamp = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
    warning = {
        "key": "metric:cpu",
        "severity": "warning",
        "title": "Høj CPU",
        "message": "CPU er høj.",
    }

    assert (
        enqueue_finding_notifications(
            warning,
            factory,
            channels,
            minimum_severity="critical",
            recorded_at=timestamp,
        )
        == 0
    )
    assert (
        enqueue_finding_notifications(
            critical_finding(),
            factory,
            channels,
            minimum_severity="critical",
            recorded_at=timestamp,
        )
        == 1
    )

    delivered = []

    def sender(channel, notification, timeout_seconds):
        delivered.append((channel["kind"], notification["event_key"], timeout_seconds))

    result = dispatch_pending_notifications(
        factory,
        channels,
        now=timestamp,
        timeout_seconds=7,
        sender=sender,
    )
    rows = list_notifications(10, factory)

    assert result == {
        "processed": 1,
        "sent": 1,
        "retrying": 0,
        "failed": 0,
        "purged": 0,
    }
    assert delivered == [("webhook", "container:api:stopped", 7)]
    assert rows[0]["status"] == "sent"
    assert rows[0]["attempts"] == 1
    assert rows[0]["sent_at"] == timestamp.isoformat()


def test_dispatch_retries_then_fails_and_redacts_credentials(tmp_path):
    factory = database_factory(tmp_path)
    secret_url = "https://example.test/very-secret-token"
    channels = {"webhook": {"kind": "webhook", "url": secret_url}}
    timestamp = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
    enqueue_finding_notifications(
        critical_finding(),
        factory,
        channels,
        recorded_at=timestamp,
    )

    def failing_sender(channel, _notification, _timeout_seconds):
        raise RuntimeError(f"could not reach {channel['url']}")

    first = dispatch_pending_notifications(
        factory,
        channels,
        now=timestamp,
        max_attempts=2,
        retry_base_seconds=60,
        sender=failing_sender,
    )
    after_first = list_notifications(1, factory)[0]
    too_early = dispatch_pending_notifications(
        factory,
        channels,
        now=timestamp + timedelta(seconds=59),
        max_attempts=2,
        retry_base_seconds=60,
        sender=failing_sender,
    )
    second = dispatch_pending_notifications(
        factory,
        channels,
        now=timestamp + timedelta(seconds=60),
        max_attempts=2,
        retry_base_seconds=60,
        sender=failing_sender,
    )
    after_second = list_notifications(1, factory)[0]

    assert first["retrying"] == 1
    assert after_first["status"] == "retrying"
    assert after_first["attempts"] == 1
    assert after_first["next_attempt_at"] == (timestamp + timedelta(seconds=60)).isoformat()
    assert secret_url not in after_first["last_error"]
    assert "[redacted]" in after_first["last_error"]
    assert too_early["processed"] == 0
    assert second["failed"] == 1
    assert after_second["status"] == "failed"
    assert after_second["attempts"] == 2
    assert after_second["next_attempt_at"] is None


def test_notification_status_never_exposes_channel_credentials(tmp_path):
    factory = database_factory(tmp_path)
    channels = {
        "webhook": {
            "kind": "webhook",
            "url": "https://example.test/secret-webhook",
        },
        "pushover": {
            "kind": "pushover",
            "token": "secret-app-token",
            "user": "secret-user-key",
        },
    }
    enqueue_finding_notifications(critical_finding(), factory, channels)

    status = notification_center_status(factory, channels)
    serialized = json.dumps(status)

    assert status["channels"] == ["pushover", "webhook"]
    assert status["pending"] == 2
    assert "secret-webhook" not in serialized
    assert "secret-app-token" not in serialized
    assert "secret-user-key" not in serialized


def test_monitoring_only_queues_notifications_for_new_deduplicated_events(tmp_path):
    factory = database_factory(tmp_path)
    flask_app = Flask(__name__)
    flask_app.config.update(
        CPU_WARNING=85,
        RAM_WARNING=85,
        DISK_WARNING=85,
        TEMP_WARNING=75,
        BACKUP_MAX_AGE_HOURS=36,
        NOTIFICATION_WEBHOOK_URL="https://example.test/hook",
        PUSHOVER_APP_TOKEN="",
        PUSHOVER_USER_KEY="",
        NOTIFICATION_MIN_SEVERITY="warning",
    )
    metrics = {
        "cpu": 90.0,
        "ram": 20.0,
        "disk": 30.0,
        "temperature": 40.0,
    }
    backup = {"recorded_at": datetime.now(timezone.utc).isoformat()}

    with flask_app.app_context():
        analyze_system(metrics, [], backup, factory)
        analyze_system(metrics, [], backup, factory)

    rows = list_notifications(10, factory)
    assert len(rows) == 1
    assert rows[0]["event_key"] == "metric:cpu"


def test_notification_retention_only_purges_terminal_history(tmp_path):
    factory = database_factory(tmp_path)
    channels = {"webhook": {"kind": "webhook", "url": "https://example.test/hook"}}
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = old + timedelta(days=31)
    enqueue_finding_notifications(critical_finding(), factory, channels, recorded_at=old)
    dispatch_pending_notifications(factory, channels, now=old, sender=lambda *_args: None)
    enqueue_finding_notifications(
        {**critical_finding(), "key": "container:db:stopped"},
        factory,
        channels,
        recorded_at=old,
    )

    purged = purge_old_notifications(factory, now=now, retention_days=30)
    rows = list_notifications(10, factory)

    assert purged == 1
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_notification_api_and_compose_do_not_embed_or_return_secrets(tmp_path):
    import app

    data_root = tmp_path / "api-data"
    secret_url = "https://example.test/api-secret"
    flask_app = app.create_app(
        {
            "TESTING": True,
            "DATA_ROOT": data_root,
            "DATABASE_PATH": data_root / "racher-os.db",
            "NOTIFICATION_WEBHOOK_URL": secret_url,
            "PUSHOVER_APP_TOKEN": "secret-token",
            "PUSHOVER_USER_KEY": "secret-user",
            "NOTIFICATION_MIN_SEVERITY": "critical",
        }
    )

    response = flask_app.test_client().get("/api/notifications")
    payload = response.get_data(as_text=True)

    assert response.status_code == 200
    assert secret_url not in payload
    assert "secret-token" not in payload
    assert "secret-user" not in payload

    repository_root = CONTROL_CENTER_ROOT.parents[1]
    compose = (repository_root / "compose/control-center/compose.yml").read_text()
    assert compose.count("NOTIFICATION_WEBHOOK_URL: ${NOTIFICATION_WEBHOOK_URL:-}") == 2
    assert compose.count("PUSHOVER_APP_TOKEN: ${PUSHOVER_APP_TOKEN:-}") == 2
    assert compose.count("PUSHOVER_USER_KEY: ${PUSHOVER_USER_KEY:-}") == 2
    assert "NOTIFICATION_RETENTION_DAYS: ${NOTIFICATION_RETENTION_DAYS:-30}" in compose
