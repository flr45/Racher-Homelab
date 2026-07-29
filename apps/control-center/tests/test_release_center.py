from pathlib import Path

from flask import Flask

import services.release_service as release_service
from rbac_extension import init_rbac
from release_extension import init_release_center

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, RBAC_VIEWER_EMAILS={"viewer@example.com"})
    init_rbac(app)
    init_release_center(app)
    return app


def test_release_center_requires_read_permission():
    assert make_app().test_client().get("/api/release").status_code == 403


def test_release_center_returns_valid_sanitized_metadata(monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(release_service, "VERSION_FILE", version_file)
    monkeypatch.setenv("RACHER_OS_COMMIT", "abcdef1234567")
    monkeypatch.setenv("RACHER_OS_CHANNEL", "stable")
    monkeypatch.setenv("RACHER_OS_BUILT_AT", "2026-07-29T09:00:00Z")

    response = make_app().test_client().get("/api/release", headers=VIEWER_HEADERS)
    payload = response.get_json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["version"] == "1.2.3"
    assert payload["version_valid"] is True
    assert payload["channel"] == "stable"
    assert payload["commit"] == "abcdef1234567"
    assert payload["read_only"] is True


def test_invalid_version_and_build_metadata_fail_closed(monkeypatch, tmp_path):
    version_file = Path(tmp_path) / "VERSION"
    version_file.write_text("latest", encoding="utf-8")
    monkeypatch.setattr(release_service, "VERSION_FILE", version_file)
    monkeypatch.setenv("RACHER_OS_COMMIT", "not a sha")
    monkeypatch.setenv("RACHER_OS_CHANNEL", "nightly")

    payload = release_service.build_release_payload()
    assert payload["version"] == "0.0.0+unknown"
    assert payload["version_valid"] is False
    assert payload["commit"] == "unknown"
    assert payload["channel"] == "development"
