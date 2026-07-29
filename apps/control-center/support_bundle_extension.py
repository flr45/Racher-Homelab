import platform
import socket
from datetime import datetime, timezone

import psutil
from flask import Blueprint, current_app, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

support_bundle_blueprint = Blueprint("support_bundle", __name__)


def build_support_bundle(config):
    disk = psutil.disk_usage("/")
    memory = psutil.virtual_memory()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "hostname": socket.gethostname()[:255],
            "system": platform.system()[:64],
            "release": platform.release()[:128],
            "machine": platform.machine()[:64],
            "python": platform.python_version()[:32],
            "cpu_count": psutil.cpu_count(),
        },
        "resources": {
            "memory_total_bytes": int(memory.total),
            "memory_used_percent": float(memory.percent),
            "disk_total_bytes": int(disk.total),
            "disk_used_percent": float(disk.percent),
            "boot_time": datetime.fromtimestamp(
                psutil.boot_time(), timezone.utc
            ).isoformat(),
        },
        "configuration": {
            "persistent_secret_configured": bool(config.get("SECRET_KEY")),
            "admin_allowlist_configured": bool(config.get("ALLOWED_EMAILS")),
            "cloudflare_configured": bool(
                config.get("CLOUDFLARE_API_TOKEN") or config.get("CLOUDFLARE_TUNNEL_ID")
            ),
            "notifications_configured": bool(
                config.get("NOTIFICATION_WEBHOOK_URL")
                or (
                    config.get("PUSHOVER_APP_TOKEN")
                    and config.get("PUSHOVER_USER_KEY")
                )
            ),
            "ssh_console_enabled": bool(config.get("SSH_CONSOLE_ENABLED", False)),
        },
        "privacy": {
            "environment_values_included": False,
            "secret_values_included": False,
            "logs_included": False,
            "file_contents_included": False,
        },
        "read_only": True,
    }


@support_bundle_blueprint.get("/api/support-bundle")
def support_bundle():
    identity = current_identity()
    if not has_permission(identity["role"], "support.export"):
        return jsonify({"error": "Brugeren har ikke adgang til Support Bundle Center."}), 403
    try:
        payload = build_support_bundle(current_app.config)
    except Exception:
        current_app.logger.exception("support_bundle_failed")
        return jsonify({"error": "Diagnosepakken kunne ikke oprettes."}), 500
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = "attachment; filename=racher-os-support.json"
    return response


def init_support_bundle_center(app):
    app.register_blueprint(support_bundle_blueprint)
