import os
from pathlib import Path

import psutil
from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

storage_center_blueprint = Blueprint("storage_center", __name__)


def collect_storage_status():
    mounts = []
    for partition in psutil.disk_partitions(all=False):
        mountpoint = Path(partition.mountpoint)
        try:
            usage = psutil.disk_usage(str(mountpoint))
        except (OSError, PermissionError):
            continue
        mounts.append(
            {
                "device": str(partition.device)[:256],
                "mountpoint": str(mountpoint)[:512],
                "filesystem": str(partition.fstype)[:64],
                "options": sorted(set(str(partition.opts).split(",")))[:32],
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "used_percent": float(usage.percent),
                "read_only": "ro" in str(partition.opts).split(","),
            }
        )
    mounts = sorted(mounts, key=lambda item: item["mountpoint"])[:100]
    warnings = []
    for mount in mounts:
        if mount["used_percent"] >= 90:
            warnings.append(
                {
                    "severity": "critical",
                    "mountpoint": mount["mountpoint"],
                    "message": "Filsystemet er mindst 90 % fyldt.",
                }
            )
        elif mount["used_percent"] >= 80:
            warnings.append(
                {
                    "severity": "warning",
                    "mountpoint": mount["mountpoint"],
                    "message": "Filsystemet er mindst 80 % fyldt.",
                }
            )
    return {
        "mounts": mounts,
        "warnings": warnings,
        "summary": {
            "mounts": len(mounts),
            "warnings": len(warnings),
            "critical": sum(1 for item in warnings if item["severity"] == "critical"),
        },
        "host_root": os.path.abspath("/")[:512],
        "read_only": True,
    }


@storage_center_blueprint.get("/api/storage")
def storage_status():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify({"error": "Brugeren har ikke adgang til Storage Center."}), 403
    try:
        payload = collect_storage_status()
    except Exception:
        payload = {
            "mounts": [],
            "warnings": [],
            "summary": {"mounts": 0, "warnings": 0, "critical": 0},
            "read_only": True,
            "degraded": True,
        }
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def init_storage_center(app):
    app.register_blueprint(storage_center_blueprint)
