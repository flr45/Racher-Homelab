from flask import Flask

import storage_center_extension
from rbac_extension import init_rbac
from storage_center_extension import init_storage_center

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, RBAC_VIEWER_EMAILS={"viewer@example.com"})
    init_rbac(app)
    init_storage_center(app)
    return app


def test_storage_center_requires_read_permission():
    assert make_app().test_client().get("/api/storage").status_code == 403


def test_storage_center_returns_mounts_and_warnings(monkeypatch):
    monkeypatch.setattr(
        storage_center_extension,
        "collect_storage_status",
        lambda: {
            "mounts": [{"mountpoint": "/data", "used_percent": 91.0}],
            "warnings": [{"severity": "critical", "mountpoint": "/data"}],
            "summary": {"mounts": 1, "warnings": 1, "critical": 1},
            "read_only": True,
        },
    )
    response = make_app().test_client().get("/api/storage", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["read_only"] is True
    assert payload["summary"]["critical"] == 1
