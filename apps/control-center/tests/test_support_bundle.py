from flask import Flask

import support_bundle_extension
from rbac_extension import init_rbac
from support_bundle_extension import init_support_bundle_center

ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}
VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    init_rbac(app)
    init_support_bundle_center(app)
    return app


def test_support_bundle_is_admin_only():
    client = make_app().test_client()
    assert client.get("/api/support-bundle", headers=VIEWER_HEADERS).status_code == 403


def test_support_bundle_excludes_sensitive_values(monkeypatch):
    monkeypatch.setattr(
        support_bundle_extension,
        "build_support_bundle",
        lambda config: {
            "host": {"hostname": "pi"},
            "resources": {},
            "configuration": {"persistent_secret_configured": True},
            "privacy": {
                "environment_values_included": False,
                "secret_values_included": False,
                "logs_included": False,
                "file_contents_included": False,
            },
            "read_only": True,
        },
    )
    response = make_app().test_client().get(
        "/api/support-bundle", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "attachment" in response.headers["Content-Disposition"]
    payload = response.get_json()
    assert payload["privacy"]["secret_values_included"] is False
    assert payload["read_only"] is True
