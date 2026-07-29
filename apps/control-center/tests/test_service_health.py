from flask import Flask

import service_health_extension
from rbac_extension import init_rbac
from service_health_extension import init_service_health_center

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, RBAC_VIEWER_EMAILS={"viewer@example.com"})
    init_rbac(app)
    init_service_health_center(app)
    return app


def test_service_health_requires_read_permission():
    assert make_app().test_client().get("/api/service-health").status_code == 403


def test_service_health_returns_failed_units(monkeypatch):
    monkeypatch.setattr(
        service_health_extension,
        "collect_service_health",
        lambda: {
            "available": True,
            "failed_units": [{"unit": "example.service", "active": "failed"}],
            "summary": {"failed": 1, "healthy": False},
            "read_only": True,
        },
    )
    response = make_app().test_client().get("/api/service-health", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["summary"]["failed"] == 1
    assert payload["read_only"] is True
