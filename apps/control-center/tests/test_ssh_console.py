from flask import Flask

import ssh_console_extension
from rbac_extension import init_rbac
from ssh_console_extension import init_ssh_console

ADMIN_HEADERS = {"Cf-Access-Authenticated-User-Email": "admin@example.com"}


def make_app(*, enabled=False):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        ALLOWED_EMAILS={"admin@example.com"},
        SSH_CONSOLE_ENABLED=enabled,
        SSH_CONSOLE_HOSTS=[
            {"id": "pi", "hostname": "raspberrypi.local", "user": "racher", "port": 22}
        ],
        SSH_KNOWN_HOSTS_PATH="/run/secrets/known_hosts",
        SSH_IDENTITY_FILE="/run/secrets/ssh_key",
        SSH_COMMAND_TIMEOUT_SECONDS=15,
    )
    init_rbac(app)
    init_ssh_console(app)
    return app


def test_status_requires_admin_and_exposes_safety():
    app = make_app()
    assert app.test_client().get("/api/ssh-console").status_code == 403
    response = app.test_client().get("/api/ssh-console", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["enabled"] is False
    assert payload["hosts"][0]["id"] == "pi"
    assert payload["safety"]["interactive_shell"] is False
    assert payload["safety"]["strict_host_key_checking"] is True


def test_execute_fails_closed_and_requires_exact_confirmation(monkeypatch):
    disabled = make_app()
    response = disabled.test_client().post(
        "/api/ssh-console/execute",
        headers=ADMIN_HEADERS,
        json={"host": "pi", "command": "uptime", "confirm": "RUN uptime ON pi"},
    )
    assert response.status_code == 503

    enabled = make_app(enabled=True)
    response = enabled.test_client().post(
        "/api/ssh-console/execute",
        headers=ADMIN_HEADERS,
        json={"host": "pi", "command": "uptime", "confirm": "wrong"},
    )
    assert response.status_code == 400


def test_execute_uses_allowlisted_command_and_no_store(monkeypatch):
    seen = {}

    def fake_execute(hosts, host_id, command_id, **kwargs):
        seen.update(host=host_id, command=command_id, timeout=kwargs["timeout_seconds"])
        return {
            "host": host_id,
            "command": command_id,
            "exit_code": 0,
            "stdout": "up 2 days",
            "stderr": "",
            "truncated": False,
        }

    monkeypatch.setattr(ssh_console_extension, "execute_diagnostic", fake_execute)
    app = make_app(enabled=True)
    response = app.test_client().post(
        "/api/ssh-console/execute",
        headers=ADMIN_HEADERS,
        json={"host": "pi", "command": "uptime", "confirm": "RUN uptime ON pi"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["result"]["stdout"] == "up 2 days"
    assert seen == {"host": "pi", "command": "uptime", "timeout": 15}


def test_arbitrary_command_is_rejected(monkeypatch):
    app = make_app(enabled=True)
    response = app.test_client().post(
        "/api/ssh-console/execute",
        headers=ADMIN_HEADERS,
        json={"host": "pi", "command": "rm-rf", "confirm": "RUN rm-rf ON pi"},
    )
    assert response.status_code == 400
    assert "ikke tilladt" in response.get_json()["error"]
