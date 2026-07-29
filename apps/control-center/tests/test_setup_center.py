from flask import Flask

from rbac_extension import init_rbac
from setup_extension import build_setup_status, init_setup_center

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app(tmp_path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        DATA_ROOT=data,
        BACKUP_ROOT=backups,
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    init_rbac(app)
    init_setup_center(app)
    return app


def test_setup_center_requires_read_permission(tmp_path):
    assert make_app(tmp_path).test_client().get("/api/setup").status_code == 403


def test_setup_center_returns_read_only_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("RACHER_OS_SECRET_KEY", "a-permanent-secret-value")
    response = make_app(tmp_path).test_client().get("/api/setup", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["required_complete"] is True
    assert payload["state"] == "ready"
    assert 0 <= payload["progress_percent"] <= 100
    assert payload["read_only"] is True
    assert all("value" not in item for item in payload["checks"])


def test_missing_secret_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("RACHER_OS_SECRET_KEY", raising=False)
    app = make_app(tmp_path)
    with app.app_context():
        payload = build_setup_status(app)
    assert payload["state"] == "setup_required"
    assert payload["next_step"] == "secret"
