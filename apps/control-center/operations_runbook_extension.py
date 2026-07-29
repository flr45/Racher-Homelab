from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

operations_runbook_blueprint = Blueprint("operations_runbook", __name__)

RUNBOOKS = (
    {
        "id": "first-boot",
        "title": "Første opstart",
        "severity": "info",
        "steps": (
            "Kør scripts/pi-preflight.sh.",
            "Udfyld .env uden standardadgangskoder.",
            "Kør bootstrap og kontroller /health.",
            "Åbn /api/setup og /api/readiness.",
        ),
    },
    {
        "id": "service-down",
        "title": "Service svarer ikke",
        "severity": "warning",
        "steps": (
            "Kontroller /api/service-health og Docker Center.",
            "Læs begrænsede containerlogs.",
            "Genstart kun den berørte container.",
            "Bekræft healthcheck og auditlog.",
        ),
    },
    {
        "id": "restore",
        "title": "Gendannelse efter fejl",
        "severity": "critical",
        "steps": (
            "Aktiver maintenance mode.",
            "Valider backup i Backup Verification Center.",
            "Kør disaster-recovery-drill.sh mod backupen.",
            "Udfør restore fra CLI og verificer alle services.",
        ),
    },
)


@operations_runbook_blueprint.get("/api/runbooks")
def api_runbooks():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke adgang til runbooks.",
                "required_permission": "system.read",
            }
        ), 403
    response = jsonify(
        {
            "runbooks": RUNBOOKS,
            "count": len(RUNBOOKS),
            "read_only": True,
            "actor": identity.get("email") or identity["role"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def init_operations_runbook_center(app):
    app.register_blueprint(operations_runbook_blueprint)
