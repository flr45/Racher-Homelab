from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("RACHER_OS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("RACHER_OS_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_ACTIONS_ENABLED", "false")

    spec = spec_from_file_location("racher_os_app", APP_PATH)
    assert spec is not None and spec.loader is not None

    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


def test_health_endpoint(monkeypatch, tmp_path):
    module = load_app(monkeypatch, tmp_path)

    response = module.app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_admin_actions_are_disabled_by_default(monkeypatch, tmp_path):
    module = load_app(monkeypatch, tmp_path)

    with module.app.test_request_context("/"):
        assert module.admin_allowed() is False


def test_database_schema_is_created(monkeypatch, tmp_path):
    module = load_app(monkeypatch, tmp_path)

    with module.database() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {"metrics", "audit_log", "events"}.issubset(tables)
