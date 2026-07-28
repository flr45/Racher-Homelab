import os

CONFIGURATION_CATALOG = (
    {"name": "RACHER_OS_SECRET_KEY", "category": "security", "secret": True, "required": True, "used_by": ["control-center"]},
    {"name": "ADMIN_ACTIONS_ENABLED", "category": "security", "secret": False, "required": False, "used_by": ["control-center"]},
    {"name": "ALLOWED_ADMIN_EMAILS", "category": "security", "secret": False, "required": False, "used_by": ["control-center"]},
    {"name": "GITHUB_REPOSITORY", "category": "github", "secret": False, "required": False, "used_by": ["control-center"]},
    {"name": "GITHUB_TOKEN", "category": "github", "secret": True, "required": False, "used_by": ["control-center"]},
    {"name": "GITHUB_CACHE_SECONDS", "category": "github", "secret": False, "required": False, "used_by": ["control-center"]},
    {"name": "GITHUB_TIMEOUT_SECONDS", "category": "github", "secret": False, "required": False, "used_by": ["control-center"]},
    {"name": "NOTIFICATION_WEBHOOK_URL", "category": "notifications", "secret": True, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "PUSHOVER_APP_TOKEN", "category": "notifications", "secret": True, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "PUSHOVER_USER_KEY", "category": "notifications", "secret": True, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "NOTIFICATION_MIN_SEVERITY", "category": "notifications", "secret": False, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "MONITOR_INTERVAL_SECONDS", "category": "monitoring", "secret": False, "required": False, "used_by": ["control-center-worker"]},
    {"name": "WORKER_HEALTH_MAX_AGE_SECONDS", "category": "monitoring", "secret": False, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "CPU_WARNING_PERCENT", "category": "monitoring", "secret": False, "required": False, "used_by": ["control-center-worker"]},
    {"name": "RAM_WARNING_PERCENT", "category": "monitoring", "secret": False, "required": False, "used_by": ["control-center-worker"]},
    {"name": "DISK_WARNING_PERCENT", "category": "monitoring", "secret": False, "required": False, "used_by": ["control-center-worker"]},
    {"name": "TEMP_WARNING_C", "category": "monitoring", "secret": False, "required": False, "used_by": ["control-center-worker"]},
    {"name": "BACKUP_MAX_AGE_HOURS", "category": "backup", "secret": False, "required": False, "used_by": ["control-center-worker"]},
    {"name": "BACKUP_ROOT", "category": "storage", "secret": False, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "RACHER_OS_DATA", "category": "storage", "secret": False, "required": False, "used_by": ["control-center", "control-center-worker"]},
    {"name": "DEPLOYMENT_ACTIONS_ENABLED", "category": "deployments", "secret": False, "required": False, "used_by": ["control-center"]},
    {"name": "TZ", "category": "runtime", "secret": False, "required": False, "used_by": ["control-center", "control-center-worker"]},
)


def _configured(value):
    return value is not None and bool(str(value).strip())


def configuration_inventory(environ=None):
    environ = os.environ if environ is None else environ
    entries = []
    for item in CONFIGURATION_CATALOG:
        configured = _configured(environ.get(item["name"]))
        entries.append(
            {
                "name": item["name"],
                "category": item["category"],
                "secret": item["secret"],
                "required": item["required"],
                "configured": configured,
                "status": "configured" if configured else ("missing" if item["required"] else "optional"),
                "used_by": list(item["used_by"]),
            }
        )
    return entries


def configuration_summary(environ=None):
    entries = configuration_inventory(environ)
    required = [entry for entry in entries if entry["required"]]
    missing = [entry for entry in required if not entry["configured"]]
    secrets = [entry for entry in entries if entry["secret"]]
    return {
        "total": len(entries),
        "configured": sum(entry["configured"] for entry in entries),
        "required": len(required),
        "missing_required": len(missing),
        "secrets": len(secrets),
        "configured_secrets": sum(entry["configured"] for entry in secrets),
        "healthy": not missing,
    }
