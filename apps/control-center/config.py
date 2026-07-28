import os
from pathlib import Path


def _csv_set(name, default="", *, lowercase=False):
    values = {
        value.strip()
        for value in os.getenv(name, default).split(",")
        if value.strip()
    }
    return {value.lower() for value in values} if lowercase else values


class Config:
    SECRET_KEY = os.getenv("RACHER_OS_SECRET_KEY")

    APP_LINKS = [
        {
            "name": "Vagtbytte",
            "url": os.getenv("VAGTBYTTE_URL", "#"),
            "icon": "🚒",
            "service": "vagtbytte",
            "version": os.getenv("VAGTBYTTE_VERSION", "–"),
        },
        {
            "name": "Indsatsbrief",
            "url": os.getenv("INDSATSBRIEF_URL", "#"),
            "icon": "🚑",
            "service": "indsatsbrief",
            "version": os.getenv("INDSATSBRIEF_VERSION", "–"),
        },
        {
            "name": "Minutregnskab",
            "url": os.getenv("MINUTREGNSKAB_URL", "#"),
            "icon": "⏱️",
            "service": "minutregnskab",
            "version": os.getenv("MINUTREGNSKAB_VERSION", "–"),
        },
        {
            "name": "Portainer",
            "url": os.getenv("PORTAINER_URL", "#"),
            "icon": "🐳",
            "service": "portainer",
            "version": "infra",
        },
        {
            "name": "Uptime Kuma",
            "url": os.getenv("UPTIME_KUMA_URL", "#"),
            "icon": "📈",
            "service": "uptime-kuma",
            "version": "infra",
        },
        {
            "name": "Nginx Proxy Manager",
            "url": os.getenv("NPM_URL", "#"),
            "icon": "🌐",
            "service": "nginx-proxy-manager",
            "version": "infra",
        },
    ]

    DOMAIN_LINKS = [
        {
            "name": "Racher OS",
            "host": os.getenv("RACHER_OS_HOST", "home.racher.dk"),
            "service": "control-center",
        },
        {
            "name": "Vagtbytte",
            "host": os.getenv("VAGTBYTTE_HOST", "vagtbytte.racher.dk"),
            "service": "vagtbytte",
        },
        {
            "name": "Indsatsbrief",
            "host": os.getenv("INDSATSBRIEF_HOST", "indsatsbrief.racher.dk"),
            "service": "indsatsbrief",
        },
        {
            "name": "Minutregnskab",
            "host": os.getenv("MINUTREGNSKAB_HOST", "minutregnskab.racher.dk"),
            "service": "minutregnskab",
        },
    ]

    BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/backups"))
    DATA_ROOT = Path(os.getenv("RACHER_OS_DATA", "/data"))
    DATABASE_PATH = DATA_ROOT / "racher-os.db"

    ADMIN_ACTIONS_ENABLED = os.getenv("ADMIN_ACTIONS_ENABLED", "false").lower() == "true"
    ALLOWED_EMAILS = _csv_set("ALLOWED_ADMIN_EMAILS", lowercase=True)
    PROTECTED_CONTAINERS = _csv_set(
        "PROTECTED_CONTAINERS", "control-center,control-center-worker,cloudflared"
    )

    CPU_WARNING = float(os.getenv("CPU_WARNING_PERCENT", "85"))
    RAM_WARNING = float(os.getenv("RAM_WARNING_PERCENT", "85"))
    DISK_WARNING = float(os.getenv("DISK_WARNING_PERCENT", "85"))
    TEMP_WARNING = float(os.getenv("TEMP_WARNING_C", "75"))
    BACKUP_MAX_AGE_HOURS = int(os.getenv("BACKUP_MAX_AGE_HOURS", "36"))

    MONITOR_INTERVAL_SECONDS = max(
        10,
        int(os.getenv("MONITOR_INTERVAL_SECONDS", "60")),
    )
    WORKER_HEALTH_MAX_AGE_SECONDS = max(
        MONITOR_INTERVAL_SECONDS * 2,
        int(os.getenv("WORKER_HEALTH_MAX_AGE_SECONDS", "180")),
    )

    GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "flr45/Racher-Homelab").strip()
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
    GITHUB_CACHE_SECONDS = min(
        3600,
        max(15, int(os.getenv("GITHUB_CACHE_SECONDS", "120"))),
    )
    GITHUB_TIMEOUT_SECONDS = min(
        30,
        max(1, int(os.getenv("GITHUB_TIMEOUT_SECONDS", "8"))),
    )

    NOTIFICATION_WEBHOOK_URL = os.getenv(
        "DISCORD_WEBHOOK_URL",
        os.getenv("NOTIFICATION_WEBHOOK_URL", ""),
    )
    PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")
    PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
    NOTIFICATION_MIN_SEVERITY = os.getenv(
        "NOTIFICATION_MIN_SEVERITY",
        "critical",
    ).lower()
    if NOTIFICATION_MIN_SEVERITY not in {"info", "warning", "critical"}:
        NOTIFICATION_MIN_SEVERITY = "critical"
    NOTIFICATION_BATCH_SIZE = min(
        100,
        max(1, int(os.getenv("NOTIFICATION_BATCH_SIZE", "20"))),
    )
    NOTIFICATION_MAX_ATTEMPTS = max(
        1,
        int(os.getenv("NOTIFICATION_MAX_ATTEMPTS", "5")),
    )
    NOTIFICATION_RETRY_BASE_SECONDS = max(
        10,
        int(os.getenv("NOTIFICATION_RETRY_BASE_SECONDS", "60")),
    )
    NOTIFICATION_HTTP_TIMEOUT_SECONDS = max(
        1,
        int(os.getenv("NOTIFICATION_HTTP_TIMEOUT_SECONDS", "10")),
    )
    NOTIFICATION_RETENTION_DAYS = min(
        365,
        max(1, int(os.getenv("NOTIFICATION_RETENTION_DAYS", "30"))),
    )
