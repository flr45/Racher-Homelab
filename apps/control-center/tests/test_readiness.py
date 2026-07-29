from pathlib import Path

from flask import Flask

import readiness_extension
from rbac_extension import init_rbac
from readiness_extension import init_readiness_center
from services.readiness_service import build_readiness_report

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


class FakeDockerClient:
    def ping(self):
        return True

    def version(self):
        return {"Version": "27.0.0"}


def base_config(tmp_path):
    data = Path(tmp_path) / "data"
    backups = Path(tmp_path) / "backups"
    plugins = Path(tmp_path) / "plugins"
    data.mkdir()
    backups.mkdir()
    plugins.mkdir()
    return {
        "DATA_ROOT": data,
        "BACKUP_ROOT": backups,
        "PLUGIN_ROOT": plugins,
        "DATABASE_PATH": data / "racher-os.db",
        "SECRET_KEY": "test-secret",
        "ALLOWED_EMAILS": {"admin@example.com"},
        "RACHER_OS_VERSION": "1.0.0",
        "CLOUDFLARE_API_TOKEN": "token",
        "CLOUDFLARE_ACCOUNT_ID": "account",
        "SSH_KNOWN_HOSTS_PATH": "/run/secrets/known_hosts",
        "SSH_IDENTITY_FILE": "/run/secrets/id_ed25519",
        "SSH_CONSOLE_HOSTS": [{"id": "pi"}],
        "NOTIFICATION_WEBHOOK_URL": "https://example.invalid/hook",
    }


def test_report_marks_missing_database_as_warning(tmp_path):
    config = base_config(tmp_path)
    report = build_readiness_report(config, docker_client_factory=FakeDockerClient)
    assert report["state"] == "ready"
    database = next(item for item in report["checks"] if item["id"] == "database")
    assert database["status"] == "warning"
    assert report["summary"]["required_failed"] == 0


def test_report_blocks_on_required_failures(tmp_path):
    config = base_config(tmp_path)
    config["DATA_ROOT"] = Path(tmp_path) / "missing"
    config["SECRET_KEY"] = ""

    def broken_docker():
        raise RuntimeError("offline")

    report = build_readiness_report(config, docker_client_factory=broken_docker)
    assert report["state"] == "blocked"
    assert report["summary"]["required_failed"] >= 3


def make_app(tmp_path, monkeypatch):
    config = base_config(tmp_path)
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
        **config,
    )
    monkeypatch.setattr(
        readiness_extension,
        "build_readiness_report",
        lambda _config: {"state": "ready", "score": 100, "checks": []},
    )
    init_rbac(app)
    init_readiness_center(app)
    return app


def test_api_requires_read_permission(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert app.test_client().get("/api/readiness").status_code == 403


def test_api_is_read_only_and_no_store(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    response = app.test_client().get("/api/readiness", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["read_only"] is True
    assert payload["report"]["state"] == "ready"
