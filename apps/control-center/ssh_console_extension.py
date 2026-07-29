import json
import os

from flask import Blueprint, current_app, jsonify, request

from rbac_extension import current_identity
from services.rbac_service import has_permission
from services.ssh_console_service import COMMANDS, execute_diagnostic, list_hosts

ssh_console_blueprint = Blueprint("ssh_console", __name__)


def _require_admin():
    identity = current_identity()
    if not has_permission(identity["role"], "ssh.manage"):
        return identity, (
            jsonify(
                {
                    "error": "Brugeren har ikke tilladelse til SSH Console.",
                    "required_permission": "ssh.manage",
                }
            ),
            403,
        )
    return identity, None


@ssh_console_blueprint.get("/api/ssh-console")
def ssh_console_status():
    identity, denied = _require_admin()
    if denied:
        return denied
    return jsonify(
        {
            "enabled": bool(current_app.config.get("SSH_CONSOLE_ENABLED", False)),
            "hosts": list_hosts(current_app.config.get("SSH_CONSOLE_HOSTS", ())),
            "commands": sorted(COMMANDS),
            "safety": {
                "interactive_shell": False,
                "arbitrary_commands": False,
                "strict_host_key_checking": True,
                "batch_mode": True,
                "max_timeout_seconds": 60,
                "output_limit_bytes": 60_000,
            },
            "actor": identity.get("email") or identity["role"],
        }
    )


@ssh_console_blueprint.post("/api/ssh-console/execute")
def ssh_console_execute():
    identity, denied = _require_admin()
    if denied:
        return denied
    if not current_app.config.get("SSH_CONSOLE_ENABLED", False):
        return jsonify({"error": "SSH Console er deaktiveret."}), 503

    payload = request.get_json(silent=True) or {}
    host_id = str(payload.get("host") or "").strip().lower()
    command_id = str(payload.get("command") or "").strip().lower()
    if payload.get("confirm") != f"RUN {command_id} ON {host_id}":
        return jsonify({"error": "Bekræftelsen er ugyldig."}), 400

    try:
        result = execute_diagnostic(
            current_app.config.get("SSH_CONSOLE_HOSTS", ()),
            host_id,
            command_id,
            known_hosts_path=current_app.config.get("SSH_KNOWN_HOSTS_PATH"),
            identity_file=current_app.config.get("SSH_IDENTITY_FILE"),
            timeout_seconds=current_app.config.get("SSH_COMMAND_TIMEOUT_SECONDS", 15),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("ssh_diagnostic_failed")
        return jsonify({"error": "SSH-diagnosen kunne ikke gennemføres."}), 502

    current_app.logger.info(
        "ssh_diagnostic_executed",
        extra={
            "actor": identity.get("email") or identity["role"],
            "ssh_host": host_id,
            "ssh_command": command_id,
            "exit_code": result["exit_code"],
        },
    )
    response = jsonify({"result": result})
    response.headers["Cache-Control"] = "no-store"
    return response


def _load_hosts():
    raw = os.getenv("SSH_CONSOLE_HOSTS_JSON", "[]")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def init_ssh_console(app):
    app.config.setdefault(
        "SSH_CONSOLE_ENABLED",
        os.getenv("SSH_CONSOLE_ENABLED", "false").lower() == "true",
    )
    app.config.setdefault("SSH_CONSOLE_HOSTS", _load_hosts())
    app.config.setdefault("SSH_KNOWN_HOSTS_PATH", os.getenv("SSH_KNOWN_HOSTS_PATH", ""))
    app.config.setdefault("SSH_IDENTITY_FILE", os.getenv("SSH_IDENTITY_FILE", ""))
    app.config.setdefault(
        "SSH_COMMAND_TIMEOUT_SECONDS",
        min(60, max(5, int(os.getenv("SSH_COMMAND_TIMEOUT_SECONDS", "15")))),
    )
    app.register_blueprint(ssh_console_blueprint)
