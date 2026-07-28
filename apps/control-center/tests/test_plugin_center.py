import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from plugin_extension import init_plugin_center
from services.plugin_service import discover_plugins, validate_manifest


def _write_manifest(root, directory, payload):
    plugin_dir = root / directory
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_validation_and_compatibility():
    plugin = validate_manifest(
        {
            "id": "metrics-exporter",
            "name": "Metrics Exporter",
            "version": "1.2.3",
            "minimum_racher_os": "1.1.0",
            "permissions": ["metrics.read", "events.read", "metrics.read"],
        },
        platform_version="1.0.0",
    )
    assert plugin["permissions"] == ["events.read", "metrics.read"]
    assert plugin["compatible"] is False
    assert plugin["execution_enabled"] is False


def test_discovery_rejects_invalid_duplicate_and_unknown_permissions(tmp_path):
    _write_manifest(
        tmp_path,
        "valid",
        {"id": "valid-plugin", "name": "Valid", "version": "1.0.0", "permissions": ["metrics.read"]},
    )
    _write_manifest(
        tmp_path,
        "duplicate",
        {"id": "valid-plugin", "name": "Duplicate", "version": "1.0.0"},
    )
    _write_manifest(
        tmp_path,
        "unsafe",
        {"id": "unsafe-plugin", "name": "Unsafe", "version": "1.0.0", "permissions": ["shell.execute"]},
    )

    result = discover_plugins(tmp_path)
    assert result["count"] == 1
    assert result["plugins"][0]["id"] == "valid-plugin"
    assert len(result["invalid"]) == 2
    assert result["execution_enabled"] is False


def test_plugin_api_dashboard_and_status(tmp_path):
    _write_manifest(
        tmp_path,
        "backup-report",
        {
            "id": "backup-report",
            "name": "Backup Report",
            "version": "1.0.0",
            "description": "Read-only backup reporting",
            "permissions": ["audit.read", "events.read"],
        },
    )
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path / "data",
            "DATABASE_PATH": tmp_path / "data" / "test.db",
            "PLUGIN_ROOT": tmp_path,
            "RACHER_OS_VERSION": "1.0.0",
        }
    )
    init_plugin_center(app)
    client = app.test_client()

    response = client.get("/api/plugins")
    assert response.status_code == 200
    body = response.get_json()
    assert body["count"] == 1
    assert body["plugins"][0]["permissions"] == ["audit.read", "events.read"]
    assert "code" not in repr(body).lower()

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["plugins"] == {
        "count": 1,
        "compatible": 1,
        "invalid": 0,
        "execution_enabled": False,
    }

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Plugin Center" in dashboard.get_data(as_text=True)


def test_missing_plugin_root_is_safe(tmp_path):
    result = discover_plugins(tmp_path / "missing")
    assert result["plugins"] == []
    assert result["execution_enabled"] is False
