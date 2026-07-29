import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

setup_blueprint = Blueprint("setup_center", __name__)


def _configured(name):
    value = os.getenv(name, "").strip()
    return bool(value and "CHANGE_ME" not in value)


def build_setup_status(app):
    data_root = Path(app.config.get("DATA_ROOT", "/data"))
    backup_root = Path(app.config.get("BACKUP_ROOT", "/backups"))
    checks = [
        {"id": "secret", "label": "Permanent app-secret", "complete": _configured("RACHER_OS_SECRET_KEY"), "required": True},
        {"id": "admin", "label": "Administrator-allowlist", "complete": bool(app.config.get("ALLOWED_EMAILS")), "required": True},
        {"id": "data", "label": "Datamappe", "complete": data_root.exists() and os.access(data_root, os.W_OK), "required": True},
        {"id": "backup", "label": "Backupmappe", "complete": backup_root.exists() and os.access(backup_root, os.W_OK), "required": True},
        {"id": "cloudflare", "label": "Cloudflare Access", "complete": _configured("CLOUDFLARE_API_TOKEN"), "required": False},
        {"id": "notifications", "label": "Notifikationer", "complete": any(_configured(name) for name in ("NOTIFICATION_WEBHOOK_URL", "DISCORD_WEBHOOK_URL", "PUSHOVER_APP_TOKEN")), "required": False},
        {"id": "backup_mirror", "label": "Ekstern backupkopi", "complete": _configured("BACKUP_MIRROR_DIR"), "required": False},
    ]
    required = [item for item in checks if item["required"]]
    completed = sum(1 for item in checks if item["complete"])
    required_complete = all(item["complete"] for item in required)
    return {
        "state": "ready" if required_complete else "setup_required",
        "required_complete": required_complete,
        "progress_percent": round(completed / len(checks) * 100),
        "checks": checks,
        "next_step": next((item["id"] for item in required if not item["complete"]), None),
        "read_only": True,
    }


@setup_blueprint.get("/api/setup")
def setup_status():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify({"error": "Brugeren har ikke adgang til Setup Center."}), 403
    payload = build_setup_status(current_app)
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def init_setup_center(app):
    app.register_blueprint(setup_blueprint)
