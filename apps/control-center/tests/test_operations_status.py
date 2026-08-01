from pathlib import Path

from flask import Flask

import operations_status_extension
from operations_status_extension import init_operations_status_center
from rbac_extension import init_rbac

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}
TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"


def make_app():
    app = Flask(__name__, template_folder=str(TEMPLATE_ROOT))
    app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
        OPERATIONS_STATUS_CONTAINERS=(
            "racher-sms-gateway",
            "vagtbytte-web",
            "cloudflared",
        ),
    )
    init_rbac(app)
    init_operations_status_center(app)
    return app


def sample_report():
    return {
        "summary": {
            "state": "healthy",
            "healthy": 5,
            "warning": 0,
            "critical": 0,
            "total": 5,
        },
        "cards": [
            {
                "id": "sms-gateway",
                "title": "SMS-gateway",
                "description": "test",
                "state": "healthy",
                "metrics": [{"label": "Modem", "value": "online"}],
                "details": [],
            }
        ],
        "containers": [
            {
                "name": "racher-sms-gateway",
                "status": "running",
                "health": "healthy",
                "state": "healthy",
            }
        ],
        "updated_at": "2026-08-01T20:30:00+02:00",
        "read_only": True,
    }


def test_operations_status_requires_read_permission():
    client = make_app().test_client()
    assert client.get("/operations-status").status_code == 403
    assert client.get("/api/operations-status").status_code == 403


def test_operations_status_page_and_api(monkeypatch):
    monkeypatch.setattr(
        operations_status_extension,
        "build_operations_status_report",
        sample_report,
    )
    client = make_app().test_client()

    page = client.get("/operations-status", headers=VIEWER_HEADERS)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert b"Driftsstatus" in page.data
    assert b"SMS-gateway" in page.data

    response = client.get("/api/operations-status", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["summary"]["state"] == "healthy"
    assert payload["read_only"] is True


def test_report_marks_nonempty_sms_queue_as_warning(monkeypatch):
    containers = [
        {
            "name": name,
            "status": "running",
            "healthy": "healthy",
            "protected": True,
        }
        for name in ("racher-sms-gateway", "vagtbytte-web", "cloudflared")
    ]
    monkeypatch.setattr(
        operations_status_extension,
        "docker_status",
        lambda include_usage=False: (containers, None),
    )

    def fake_fetch(url, timeout=None):
        if "sms-gateway" in url:
            return {
                "available": True,
                "status_code": 200,
                "latency_ms": 5,
                "error": None,
                "payload": {
                    "status": "ok",
                    "modem": {"state": "online"},
                    "gateway": {
                        "database": "online",
                        "outbox_pending": 1,
                        "outbox_failed": 0,
                    },
                },
            }
        return {
            "available": True,
            "status_code": 200,
            "latency_ms": 4,
            "error": None,
            "payload": {"status": "ok"},
        }

    monkeypatch.setattr(operations_status_extension, "fetch_json", fake_fetch)
    monkeypatch.setattr(
        operations_status_extension,
        "build_backup_verification_report",
        lambda: {
            "status": "verified",
            "validation": {"errors": []},
            "age_hours": 2,
            "max_age_hours": 30,
        },
    )
    monkeypatch.setattr(
        operations_status_extension,
        "collect_vagtbytte_backup",
        lambda: {
            "status": "verified",
            "state": "healthy",
            "latest": "backup.enc",
            "age_hours": 2,
        },
    )

    app = make_app()
    with app.app_context():
        report = operations_status_extension.build_operations_status_report()

    sms = next(card for card in report["cards"] if card["id"] == "sms-gateway")
    assert sms["state"] == "warning"
    assert report["summary"]["state"] == "warning"


def test_navigation_module_is_registered():
    make_app()
    ids = [
        item["id"]
        for item in operations_status_extension.module_registry_service.MODULES
    ]
    assert "operations-status" in ids
