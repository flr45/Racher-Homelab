import importlib
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


def test_create_app_applies_config_overrides(monkeypatch, tmp_path):
    _, flask_app = load_app(monkeypatch, tmp_path)

    assert flask_app.config["TESTING"] is True
    assert flask_app.config["SECRET_KEY"] == "test-secret-key"
