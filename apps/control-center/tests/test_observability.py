import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from observability_extension import init_observability
from services.observability_service import normalize_logs, validate_container_name


def test_log_redaction_and_search():
    raw = "\n".join(
        [
            "2026-07-28T20:00:00Z Authorization: Bearer abc123 started",
            "2026-07-28T20:00:01Z password=hunter2 database connected",
            "2026-07-28T20:00:02Z ordinary healthcheck",
            "2026-07-28T20:00:03Z https://user:secret@example.test/path failed",
        ]
    )
    entries = normalize_logs(raw, query="", limit=10)
    serialized = repr(entries)
    assert "abc123" not in serialized
    assert "hunter2" not in serialized
    assert "user:secret" not in serialized
    assert serialized.count("[REDACTED]") == 3

    filtered = normalize_logs(raw, query="health", limit=10)
    assert len(filtered) == 1
    assert filtered[0]["message"] == "ordinary healthcheck"


def test_container_name_validation():
    assert validate_container_name("control-center_1") == "control-center_1"
    for invalid in ("../secret", "name/child", "", " name"):
        try:
            validate_container_name(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid container name: {invalid!r}")


def test_history_api_dashboard_and_status(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_observability(app)
    client = app.test_client()

    history = client.get("/api/observability/history?hours=9999")
    assert history.status_code == 200
    body = history.get_json()
    assert body["hours"] == 720
    assert body["count"] == 0

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["observability"] == {
        "history_points": 0,
        "retention_days": 30,
        "logs_redacted": True,
    }

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    html = dashboard.get_data(as_text=True)
    assert "Metrics &amp; Log Explorer" in html or "Metrics & Log Explorer" in html
    assert "/api/observability/history?hours=24" in html


def test_log_api_hides_backend_error(monkeypatch, tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_observability(app)

    def fail(*args, **kwargs):
        raise RuntimeError("docker socket contains secret-token")

    monkeypatch.setattr("observability_extension.container_logs", fail)
    response = app.test_client().get("/api/observability/logs/control-center")
    assert response.status_code == 503
    assert response.get_json() == {"error": "Logdata kunne ikke hentes."}
    assert "secret-token" not in response.get_data(as_text=True)
