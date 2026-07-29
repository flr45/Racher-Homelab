import re
import subprocess

from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

update_center_blueprint = Blueprint("update_center", __name__)
PACKAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")


def collect_update_status(runner=subprocess.run):
    completed = runner(
        ["apt", "list", "--upgradable"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    packages = []
    for line in (completed.stdout or "").splitlines()[1:501]:
        if "/" not in line:
            continue
        name, remainder = line.split("/", 1)
        if not PACKAGE_PATTERN.fullmatch(name):
            continue
        fields = remainder.split()
        packages.append(
            {
                "name": name,
                "candidate": fields[1][:128] if len(fields) > 1 else "unknown",
                "architecture": fields[2][:32] if len(fields) > 2 else "unknown",
            }
        )
    available = completed.returncode == 0
    return {
        "available": available,
        "packages": packages,
        "summary": {"upgradable": len(packages), "current": available and not packages},
        "actions_enabled": False,
        "read_only": True,
    }


@update_center_blueprint.get("/api/updates")
def update_status():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify({"error": "Brugeren har ikke adgang til Update Center."}), 403
    try:
        payload = collect_update_status()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        payload = {
            "available": False,
            "packages": [],
            "summary": {"upgradable": 0, "current": False},
            "actions_enabled": False,
            "read_only": True,
            "degraded": True,
        }
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def init_update_center(app):
    app.register_blueprint(update_center_blueprint)
