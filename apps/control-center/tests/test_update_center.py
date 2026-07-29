from flask import Flask

import update_center_extension
from rbac_extension import init_rbac
from update_center_extension import init_update_center

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, RBAC_VIEWER_EMAILS={"viewer@example.com"})
    init_rbac(app)
    init_update_center(app)
    return app


def test_update_center_requires_read_permission():
    assert make_app().test_client().get("/api/updates").status_code == 403


def test_update_center_is_read_only(monkeypatch):
    monkeypatch.setattr(
        update_center_extension,
        "collect_update_status",
        lambda: {
            "available": True,
            "packages": [{"name": "curl", "candidate": "1.2.3", "architecture": "arm64"}],
            "summary": {"upgradable": 1, "current": False},
            "actions_enabled": False,
            "read_only": True,
        },
    )
    response = make_app().test_client().get("/api/updates", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["summary"]["upgradable"] == 1
    assert payload["actions_enabled"] is False
