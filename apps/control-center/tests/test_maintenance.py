import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_CENTER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_CENTER_ROOT))

from maintenance_extension import init_maintenance  # noqa: E402
from services.database_service import open_database  # noqa: E402
from services.maintenance_service import (  # noqa: E402
    disable_maintenance,
    enable_maintenance,
    maintenance_status,
)


def database_factory(tmp_path):
    data_root = tmp_path / "data"
    database_path = data_root / "racher-os.db"
    return lambda: open_database(data_root, database_path)


def configured_app(tmp_path):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        DATA_ROOT=tmp_path / "data",
        DATABASE_PATH=tmp_path / "data" / "racher-os.db",
        ADMIN_ACTIONS_ENABLED=True,
        ALLOWED_EMAILS={"admin@example.com"},
    )

    @app.get("/")
    def index():
        return "<html><body><main>Dashboard</main></body></html>"

    @app.get("/api/status")
    def status():
        return jsonify({"status": "ok"})

    @app.post("/api/write")
    def write():
        return jsonify({"ok": True})

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    init_maintenance(app)
    return app


def admin_headers(csrf="csrf-token"):
    return {
        "Cf-Access-Authenticated-User-Email": "admin@example.com",
        "X-CSRF-Token": csrf,
    }


def set_csrf(client, value="csrf-token"):
    with client.session_transaction() as session:
        session["csrf_token"] = value


def test_maintenance_lifecycle_and_automatic_expiry(tmp_path):
    factory = database_factory(tmp_path)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    enabled = enable_maintenance("Databasearbejde", 30, "admin", factory, enabled_at=started)
    active = maintenance_status(factory, checked_at=started + timedelta(minutes=10))
    expired = maintenance_status(factory, checked_at=started + timedelta(minutes=31))

    assert enabled["enabled"] is True
    assert active["remaining_seconds"] == 20 * 60
    assert expired["enabled"] is False
    with factory() as connection:
        row = connection.execute("SELECT * FROM maintenance_mode WHERE id = 1").fetchone()
    assert row["enabled"] == 0
    assert row["disabled_by"] == "automatic-expiry"


def test_duration_is_bounded_and_manual_disable_is_idempotent(tmp_path):
    factory = database_factory(tmp_path)

    try:
        enable_maintenance("test", 0, "admin", factory)
    except ValueError as exc:
        assert "mellem 1" in str(exc)
    else:
        raise AssertionError("Expected invalid duration to fail")

    disabled = disable_maintenance("admin", factory)
    assert disabled["enabled"] is False


def test_api_requires_admin_and_csrf(tmp_path):
    client = configured_app(tmp_path).test_client()

    response = client.post(
        "/api/maintenance",
        json={"message": "test", "duration_minutes": 30},
    )
    assert response.status_code == 403

    set_csrf(client)
    response = client.post(
        "/api/maintenance",
        json={"message": "test", "duration_minutes": 30},
        headers=admin_headers("wrong"),
    )
    assert response.status_code == 403


def test_api_banner_status_and_write_protection(tmp_path):
    app = configured_app(tmp_path)
    client = app.test_client()
    set_csrf(client)

    response = client.post(
        "/api/maintenance",
        json={"message": "<script>alert(1)</script>", "duration_minutes": 30},
        headers=admin_headers(),
    )
    assert response.status_code == 201

    status = client.get("/api/status").get_json()
    assert status["maintenance"]["enabled"] is True

    page = client.get("/").get_data(as_text=True)
    assert "Vedligeholdelsestilstand aktiv" in page
    assert "&lt;script&gt;" in page
    assert "<script>" not in page

    blocked = client.post("/api/write")
    assert blocked.status_code == 503
    assert blocked.get_json()["maintenance"]["enabled"] is True

    health = client.get("/health")
    assert health.status_code == 200

    allowed = client.post("/api/write", headers={"Cf-Access-Authenticated-User-Email": "admin@example.com"})
    assert allowed.status_code == 200

    disabled = client.delete("/api/maintenance", headers=admin_headers())
    assert disabled.status_code == 200
    assert disabled.get_json()["maintenance"]["enabled"] is False


def test_maintenance_schema_is_created(tmp_path):
    factory = database_factory(tmp_path)
    with factory() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "maintenance_mode" in tables
