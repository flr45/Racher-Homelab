from flask import Flask

import network_center_extension
from network_center_extension import init_network_center
from rbac_extension import init_rbac

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, RBAC_VIEWER_EMAILS={"viewer@example.com"})
    init_rbac(app)
    init_network_center(app)
    return app


def test_network_center_requires_read_permission():
    assert make_app().test_client().get("/api/network").status_code == 403


def test_network_center_returns_sanitized_read_only_status(monkeypatch):
    monkeypatch.setattr(
        network_center_extension,
        "collect_network_status",
        lambda: {
            "hostname": "pi",
            "interfaces": [{"name": "eth0", "up": True, "addresses": []}],
            "listening_ports": [{"address": "0.0.0.0", "port": 443, "pid": 1}],
            "summary": {"interfaces": 1, "interfaces_up": 1, "listening_ports": 1},
            "read_only": True,
        },
    )
    response = make_app().test_client().get("/api/network", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["read_only"] is True
    assert payload["interfaces"][0]["name"] == "eth0"
    assert payload["listening_ports"][0]["port"] == 443
