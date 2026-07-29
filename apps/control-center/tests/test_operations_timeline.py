from pathlib import Path

from flask import Flask

from operations_timeline_extension import init_operations_timeline
from rbac_extension import init_rbac
from services.database_service import open_database

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app(tmp_path):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        DATA_ROOT=Path(tmp_path),
        DATABASE_PATH=Path(tmp_path) / "racher-os.db",
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    init_rbac(app)
    init_operations_timeline(app)
    return app


def seed(app):
    with open_database(app.config["DATA_ROOT"], app.config["DATABASE_PATH"]) as connection:
        connection.execute(
            "INSERT INTO audit_log (recorded_at, actor, action, target, success, message) VALUES (?, ?, ?, ?, ?, ?)",
            ("2099-01-01T10:00:00+00:00", "admin@example.com", "container.restart", "api", 1, "Restarted"),
        )
        connection.execute(
            "INSERT INTO events (recorded_at, event_key, severity, title, message) VALUES (?, ?, ?, ?, ?)",
            ("2099-01-01T11:00:00+00:00", "disk.high", "critical", "Disk pressure", "Disk at 95%"),
        )
        connection.execute(
            "INSERT INTO notifications (created_at, event_key, channel, severity, title, message, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2099-01-01T12:00:00+00:00", "disk.high", "discord", "critical", "Alert sent", "Disk at 95%", "sent"),
        )
        connection.execute(
            "INSERT INTO deployment_history (recorded_at, container_name, change_type, image_reference, status) VALUES (?, ?, ?, ?, ?)",
            ("2099-01-01T13:00:00+00:00", "api", "image_changed", "repo/api:2.0", "running"),
        )
        connection.commit()


def test_timeline_requires_read_permission(tmp_path):
    app = make_app(tmp_path)
    response = app.test_client().get("/api/operations-timeline")
    assert response.status_code == 403


def test_timeline_combines_sorts_and_summarizes(tmp_path):
    app = make_app(tmp_path)
    seed(app)
    response = app.test_client().get(
        "/api/operations-timeline?since_hours=8760&limit=20",
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["source"] for item in payload["items"]] == [
        "deployment",
        "notification",
        "event",
        "audit",
    ]
    assert payload["summary"]["total"] == 4
    assert payload["summary"]["by_severity"]["critical"] == 2
    assert payload["summary"]["by_severity"]["success"] == 2


def test_timeline_filters_query_and_rejects_unknown_values(tmp_path):
    app = make_app(tmp_path)
    seed(app)
    client = app.test_client()
    filtered = client.get(
        "/api/operations-timeline?source=event&severity=critical&q=disk&since_hours=8760",
        headers=VIEWER_HEADERS,
    )
    assert filtered.status_code == 200
    assert len(filtered.get_json()["items"]) == 1
    invalid = client.get(
        "/api/operations-timeline?source=unknown",
        headers=VIEWER_HEADERS,
    )
    assert invalid.status_code == 400


def test_csv_export_is_bounded_and_no_store(tmp_path):
    app = make_app(tmp_path)
    seed(app)
    response = app.test_client().get(
        "/api/operations-timeline/export.csv?since_hours=8760&limit=9999",
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "attachment;" in response.headers["Content-Disposition"]
    text = response.get_data(as_text=True)
    assert text.startswith("timestamp,source,severity")
    assert "Disk pressure" in text
