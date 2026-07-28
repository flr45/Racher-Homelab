import os
import secrets
import time

from flask import Blueprint, current_app, jsonify, request

from rbac_extension import current_identity
from services.backup_service import BackupNotFoundError, backups, validate_backup
from services.rbac_service import has_permission

restore_ui_blueprint = Blueprint("restore_ui", __name__)
_STAGED_RESTORES = {}


def _identity_or_denied(permission):
    identity = current_identity()
    if has_permission(identity["role"], permission):
        return identity, None
    return None, (
        jsonify(
            {
                "error": "Brugeren har ikke tilladelse til handlingen.",
                "required_permission": permission,
            }
        ),
        403,
    )


def _purge_expired(now=None):
    now = time.time() if now is None else now
    expired = [token for token, item in _STAGED_RESTORES.items() if item["expires_at"] <= now]
    for token in expired:
        _STAGED_RESTORES.pop(token, None)


@restore_ui_blueprint.get("/api/restore")
def restore_status():
    identity = current_identity()
    _purge_expired()
    return jsonify(
        {
            "enabled": bool(current_app.config.get("RESTORE_ACTIONS_ENABLED", False)),
            "can_manage": has_permission(identity["role"], "restore.manage"),
            "backups": backups(limit=50),
            "staged_count": len(_STAGED_RESTORES),
            "safety": {
                "validation_required": True,
                "confirmation_format": "RESTORE <backup-name>",
                "stage_expires_seconds": current_app.config.get("RESTORE_STAGE_TTL_SECONDS", 600),
                "execution_mode": "cli-only",
                "automatic_execution": False,
            },
        }
    )


@restore_ui_blueprint.post("/api/restore/validate")
def validate_restore_backup():
    _, denied = _identity_or_denied("restore.manage")
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("backup_name") or "").strip()
    try:
        result = validate_backup(name)
    except BackupNotFoundError:
        return jsonify({"error": "Backup blev ikke fundet."}), 404
    return jsonify({"validation": result}), 200 if result["valid"] else 409


@restore_ui_blueprint.post("/api/restore/stage")
def stage_restore():
    identity, denied = _identity_or_denied("restore.manage")
    if denied:
        return denied
    if not current_app.config.get("RESTORE_ACTIONS_ENABLED", False):
        return jsonify({"error": "Restore-handlinger er deaktiveret."}), 503

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("backup_name") or "").strip()
    confirmation = str(payload.get("confirm") or "").strip()
    if confirmation != f"RESTORE {name}" or not name:
        return jsonify({"error": "Bekræftelsen skal være RESTORE efterfulgt af backupnavnet."}), 400

    try:
        validation = validate_backup(name)
    except BackupNotFoundError:
        return jsonify({"error": "Backup blev ikke fundet."}), 404
    if not validation["valid"]:
        return jsonify({"error": "Backup validerede ikke.", "validation": validation}), 409

    _purge_expired()
    token = secrets.token_urlsafe(32)
    ttl = current_app.config.get("RESTORE_STAGE_TTL_SECONDS", 600)
    staged = {
        "token": token,
        "backup_name": name,
        "actor": identity.get("email") or identity["role"],
        "created_at": time.time(),
        "expires_at": time.time() + ttl,
        "execution_mode": "cli-only",
    }
    _STAGED_RESTORES[token] = staged
    current_app.logger.warning(
        "restore_staged",
        extra={"event": "restore_staged", "backup_name": name, "actor": staged["actor"]},
    )
    return jsonify({"staged_restore": staged}), 201


def init_restore_ui(app):
    app.config.setdefault(
        "RESTORE_ACTIONS_ENABLED",
        os.getenv("RESTORE_ACTIONS_ENABLED", "false").lower() == "true",
    )
    app.config.setdefault(
        "RESTORE_STAGE_TTL_SECONDS",
        min(3600, max(60, int(os.getenv("RESTORE_STAGE_TTL_SECONDS", "600")))),
    )
    app.register_blueprint(restore_ui_blueprint)
