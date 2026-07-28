import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from configuration_extension import init_configuration_center
from services.configuration_service import configuration_inventory, configuration_summary


def test_inventory_never_returns_values():
    secret = "super-secret-token"
    entries = configuration_inventory(
        {
            "RACHER_OS_SECRET_KEY": "configured",
            "GITHUB_TOKEN": secret,
            "CPU_WARNING_PERCENT": "85",
            "UNKNOWN_SECRET": "must-not-appear",
        }
    )

    serialized = repr(entries)
    assert secret not in serialized
    assert "must-not-appear" not in serialized
    assert "UNKNOWN_SECRET" not in serialized
    github = next(item for item in entries if item["name"] == "GITHUB_TOKEN")
    assert github == {
        "name": "GITHUB_TOKEN",
        "category": "github",
        "secret": True,
        "required": False,
        "configured": True,
        "status": "configured",
        "used_by": ["control-center"],
    }


def test_summary_marks_missing_required_secret():
    summary = configuration_summary({})
    assert summary["healthy"] is False
    assert summary["missing_required"] == 1

    configured = configuration_summary({"RACHER_OS_SECRET_KEY": "set"})
    assert configured["healthy"] is True
    assert configured["missing_required"] == 0


def test_api_and_dashboard_expose_metadata_only(monkeypatch, tmp_path):
    monkeypatch.setenv("RACHER_OS_SECRET_KEY", "never-return-this")
    monkeypatch.setenv("GITHUB_TOKEN", "also-never-return-this")

    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_configuration_center(app)
    client = app.test_client()

    response = client.get("/api/configuration")
    assert response.status_code == 200
    body = response.get_json()
    serialized = response.get_data(as_text=True)
    assert "never-return-this" not in serialized
    assert "also-never-return-this" not in serialized
    assert body["configuration_center"]["healthy"] is True
    assert any(item["name"] == "GITHUB_TOKEN" for item in body["variables"])

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    html = dashboard.get_data(as_text=True)
    assert "Secrets &amp; Environment" in html or "Secrets & Environment" in html
    assert "never-return-this" not in html


def test_status_contains_summary_without_inventory(monkeypatch, tmp_path):
    monkeypatch.setenv("RACHER_OS_SECRET_KEY", "configured")
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_configuration_center(app)
    response = app.test_client().get("/api/status")
    assert response.status_code == 200
    body = response.get_json()
    assert body["configuration_center"]["healthy"] is True
    assert "variables" not in body["configuration_center"]
