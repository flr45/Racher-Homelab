from pathlib import Path

SAFE_DEFAULTS = {
    "ADMIN_ACTIONS_ENABLED": False,
    "DEPLOYMENT_ACTIONS_ENABLED": False,
    "RESTORE_ACTIONS_ENABLED": False,
    "SSH_CONSOLE_ENABLED": False,
    "DOCKER_CLEANUP_ENABLED": False,
}


def build_configuration_drift_report(config):
    checks = []
    for key, expected in SAFE_DEFAULTS.items():
        actual = bool(config.get(key, False))
        checks.append(
            {
                "key": key,
                "status": "match" if actual is expected else "drift",
                "expected_safe_state": expected,
                "actual_enabled": actual,
                "sensitive": False,
            }
        )

    for key in ("DATA_ROOT", "BACKUP_ROOT", "PLUGIN_ROOT"):
        value = Path(config.get(key, "/"))
        checks.append(
            {
                "key": key,
                "status": "match" if value.is_absolute() else "drift",
                "expected_safe_state": "absolute_path",
                "actual_enabled": None,
                "sensitive": False,
            }
        )

    secret_present = bool(config.get("SECRET_KEY"))
    checks.append(
        {
            "key": "SECRET_KEY",
            "status": "match" if secret_present else "drift",
            "expected_safe_state": "configured",
            "actual_enabled": secret_present,
            "sensitive": True,
        }
    )

    drift_count = sum(item["status"] == "drift" for item in checks)
    return {
        "status": "drift_detected" if drift_count else "aligned",
        "drift_count": drift_count,
        "check_count": len(checks),
        "checks": checks,
        "read_only": True,
    }
