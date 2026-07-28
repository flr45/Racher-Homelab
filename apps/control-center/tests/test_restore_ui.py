from flask import Flask

import restore_ui_extension
from rbac_extension import init_rbac
from restore_ui_extension import init_restore_ui

ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}


def make_app(monkeypatch, *, enabled=False):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        ALLOWED_EMAILS={"admin@example.com"},
        RESTORE_ACTIONS_ENABLED=enabled,
        RESTORE_STAGE_TTL_SECONDS=600,
    )
    monkeypatch.setattr(restore_ui_extension, "backups", lambda limit=50: [{"name": "backup-1"}])
    monkeypatch.setattr(
        restore_ui_extension,
        "validate_backup",
        lambda name: {
            "name": name,
            "valid": True,
            "missing": [],
            "errors": [],
            "checked_files": ["postgres.dump"],
            "manifest": {"format_version": 1},
        },
    )
    restore_ui_extension._STAGED_RESTORES.clear()
    init_rbac(app)
    init_restore_ui(app)
    return app


def test_status_is_read_only_and_reports_safety(monkeypatch):
    app = make_app(monkeypatch)
    response = app.test_client().get("/api/restore", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["enabled"] is False
    assert payload["can_manage"] is True
    assert payload["safety"]["execution_mode"] == "cli-only"
    assert payload["safety"]["automatic_execution"] is False


def test_anonymous_cannot_validate_or_stage(monkeypatch):
    app = make_app(monkeypatch, enabled=True)
    client = app.test_client()
    validate = client.post("/api/restore/validate", json={"backup_name": "backup-1"})
    stage = client.post(
        "/api/restore/stage",
        json={"backup_name": "backup-1", "confirm": "RESTORE backup-1"},
    )
    assert validate.status_code == 403
    assert stage.status_code == 403


def test_restore_actions_fail_closed(monkeypatch):
    app = make_app(monkeypatch)
    response = app.test_client().post(
        "/api/restore/stage",
        headers=ADMIN_HEADERS,
        json={"backup_name": "backup-1", "confirm": "RESTORE backup-1"},
    )
    assert response.status_code == 503


def test_confirmation_must_match_backup_name(monkeypatch):
    app = make_app(monkeypatch, enabled=True)
    response = app.test_client().post(
        "/api/restore/stage",
        headers=ADMIN_HEADERS,
        json={"backup_name": "backup-1", "confirm": "RESTORE wrong"},
    )
    assert response.status_code == 400


def test_invalid_backup_cannot_be_staged(monkeypatch):
    app = make_app(monkeypatch, enabled=True)
    monkeypatch.setattr(
        restore_ui_extension,
        "validate_backup",
        lambda name: {
            "name": name,
            "valid": False,
            "missing": ["postgres.dump"],
            "errors": [],
            "checked_files": [],
            "manifest": {"format_version": 1},
        },
    )
    response = app.test_client().post(
        "/api/restore/stage",
        headers=ADMIN_HEADERS,
        json={"backup_name": "backup-1", "confirm": "RESTORE backup-1"},
    )
    assert response.status_code == 409


def test_valid_backup_creates_expiring_cli_only_stage(monkeypatch):
    app = make_app(monkeypatch, enabled=True)
    response = app.test_client().post(
        "/api/restore/stage",
        headers=ADMIN_HEADERS,
        json={"backup_name": "backup-1", "confirm": "RESTORE backup-1"},
    )
    assert response.status_code == 201
    staged = response.get_json()["staged_restore"]
    assert staged["backup_name"] == "backup-1"
    assert staged["actor"] == "admin@example.com"
    assert staged["execution_mode"] == "cli-only"
    assert staged["expires_at"] > staged["created_at"]
    assert len(staged["token"]) >= 32
