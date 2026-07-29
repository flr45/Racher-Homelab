from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify

from rbac_extension import current_identity
from services.backup_service import backups, validate_backup
from services.rbac_service import has_permission

backup_verification_blueprint = Blueprint("backup_verification", __name__)


def build_backup_verification_report():
    items = backups(limit=1)
    if not items:
        return {
            "status": "missing",
            "latest": None,
            "validation": None,
            "age_hours": None,
            "max_age_hours": max(
                1, int(current_app.config.get("BACKUP_MAX_AGE_HOURS", 36))
            ),
            "read_only": True,
        }

    latest = items[0]
    validation = validate_backup(latest["name"])
    recorded_at = datetime.fromisoformat(latest["recorded_at"])
    age_hours = max(
        0,
        round((datetime.now(timezone.utc) - recorded_at).total_seconds() / 3600, 1),
    )
    max_age = max(1, int(current_app.config.get("BACKUP_MAX_AGE_HOURS", 36)))
    status = "verified"
    if not validation["valid"]:
        status = "invalid"
    elif age_hours > max_age:
        status = "stale"

    return {
        "status": status,
        "latest": latest,
        "validation": {
            "valid": validation["valid"],
            "missing": validation["missing"],
            "errors": validation["errors"],
            "checked_files": validation["checked_files"],
        },
        "age_hours": age_hours,
        "max_age_hours": max_age,
        "read_only": True,
    }


@backup_verification_blueprint.get("/api/backup-verification")
def api_backup_verification():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke adgang til backupverifikation.",
                "required_permission": "system.read",
            }
        ), 403

    response = jsonify(
        {
            "report": build_backup_verification_report(),
            "actor": identity.get("email") or identity["role"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def init_backup_verification_center(app):
    app.register_blueprint(backup_verification_blueprint)
