import csv
import io

from flask import Blueprint, Response, current_app, jsonify, request

from rbac_extension import current_identity
from services.audit_service import list_audit_entries
from services.database_service import open_database
from services.rbac_service import has_permission


audit_export_blueprint = Blueprint("audit_export", __name__)


def _database_factory():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def _limit():
    try:
        value = int(request.args.get("limit", "500"))
    except ValueError as exc:
        raise ValueError("limit skal være et heltal") from exc
    return min(max(value, 1), 5000)


def _require_permission():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return None, (
            jsonify(
                {
                    "error": "Brugeren har ikke adgang til audit-eksport.",
                    "required_permission": "system.read",
                }
            ),
            403,
        )
    return identity, None


@audit_export_blueprint.get("/api/audit-export")
def api_audit_export():
    identity, denied = _require_permission()
    if denied:
        return denied
    try:
        limit = _limit()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    entries = list_audit_entries(limit, _database_factory)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=("id", "recorded_at", "actor", "action", "target", "success", "message"),
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(entries)
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="racher-os-audit.csv"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Audit-Actor"] = identity.get("email") or identity["role"]
    return response


def init_audit_export_center(app):
    app.register_blueprint(audit_export_blueprint)
