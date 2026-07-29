from pathlib import Path

from flask import Flask

from configuration_drift_extension import init_configuration_drift_center
from rbac_extension import init_rbac
from services.configuration_drift_service import build_configuration_drift_report

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="permanent-secret",
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
        DATA_ROOT=Path("/data"),
        BACKUP_ROOT=Path("/backups"),
        PLUGIN_ROOT=Path("/plugins"),
    )
    init_rbac(app)
    init_configuration_drift_center(app)
    return app


def test_configuration_drift_requires_read_permission():
    assert make_app().test_client().get("/api/configuration-drift").status_code == 403


def test_safe_defaults_are_aligned_and_no_store():
    response = make_app().test_client().get(
        "/api/configuration-drift", headers=VIEWER_HEADERS
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    report = response.get_json()["report"]
    assert report["status"] == "aligned"
    assert report["drift_count"] == 0
    assert report["read_only"] is True


def test_enabled_admin_action_is_reported_without_secret_value():
    app = make_app()
    app.config["ADMIN_ACTIONS_ENABLED"] = True
    report = build_configuration_drift_report(app.config)
    assert report["status"] == "drift_detected"
    assert report["drift_count"] == 1
    secret = next(item for item in report["checks"] if item["key"] == "SECRET_KEY")
    assert secret["sensitive"] is True
    assert "value" not in secret
