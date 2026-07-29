import subprocess

from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

service_health_blueprint = Blueprint("service_health", __name__)


def collect_service_health(runner=subprocess.run):
    completed = runner(
        ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    failed = []
    for line in (completed.stdout or "").splitlines()[:100]:
        parts = line.split(None, 4)
        if not parts:
            continue
        failed.append(
            {
                "unit": parts[0][:256],
                "load": parts[1][:32] if len(parts) > 1 else "unknown",
                "active": parts[2][:32] if len(parts) > 2 else "unknown",
                "sub": parts[3][:32] if len(parts) > 3 else "unknown",
                "description": parts[4][:512] if len(parts) > 4 else "",
            }
        )
    available = completed.returncode in {0, 1}
    return {
        "available": available,
        "failed_units": failed,
        "summary": {
            "failed": len(failed),
            "healthy": available and not failed,
        },
        "read_only": True,
    }


@service_health_blueprint.get("/api/service-health")
def service_health_status():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify({"error": "Brugeren har ikke adgang til Service Health Center."}), 403
    try:
        payload = collect_service_health()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        payload = {
            "available": False,
            "failed_units": [],
            "summary": {"failed": 0, "healthy": False},
            "read_only": True,
            "degraded": True,
        }
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def init_service_health_center(app):
    app.register_blueprint(service_health_blueprint)
