from datetime import datetime, timedelta, timezone

from flask import current_app

from services.backup_service import newest_backup
from services.docker_service import docker_status
from services.event_service import append_event
from services.metrics_service import record_metrics, system_metrics
from services.notification_service import (
    configured_channels,
    enqueue_finding_notifications,
)


def analyze_system(metrics, containers, backup, database_factory, docker_error=None):
    findings = []

    if docker_error:
        findings.append(
            {
                "key": "docker:unavailable",
                "severity": "critical",
                "title": "Docker kan ikke kontaktes",
                "message": str(docker_error)[:500],
            }
        )

    for key, value, threshold, label in [
        ("cpu", metrics["cpu"], current_app.config["CPU_WARNING"], "CPU"),
        ("ram", metrics["ram"], current_app.config["RAM_WARNING"], "RAM"),
        ("disk", metrics["disk"], current_app.config["DISK_WARNING"], "SSD"),
    ]:
        if value >= threshold:
            findings.append(
                {
                    "key": f"metric:{key}",
                    "severity": "warning",
                    "title": f"Høj {label}-belastning",
                    "message": f"{label} er på {value}% (grænse {threshold}%).",
                }
            )

    if (
        metrics["temperature"] is not None
        and metrics["temperature"] >= current_app.config["TEMP_WARNING"]
    ):
        findings.append(
            {
                "key": "metric:temperature",
                "severity": "warning",
                "title": "Høj temperatur",
                "message": f"Servertemperaturen er {metrics['temperature']}°C.",
            }
        )

    for container in containers:
        if container["status"] != "running":
            findings.append(
                {
                    "key": f"container:{container['name']}:stopped",
                    "severity": "critical",
                    "title": "Container stoppet",
                    "message": f"{container['name']} har status {container['status']}.",
                }
            )
        elif container.get("healthy") == "unhealthy":
            findings.append(
                {
                    "key": f"container:{container['name']}:unhealthy",
                    "severity": "critical",
                    "title": "Container unhealthy",
                    "message": f"{container['name']} fejler sit healthcheck.",
                }
            )

    if not backup:
        findings.append(
            {
                "key": "backup:missing",
                "severity": "warning",
                "title": "Ingen backup fundet",
                "message": "Backupmappen indeholder ingen registreret backup.",
            }
        )
    else:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(backup["recorded_at"])
        if age > timedelta(hours=current_app.config["BACKUP_MAX_AGE_HOURS"]):
            findings.append(
                {
                    "key": "backup:old",
                    "severity": "warning",
                    "title": "Backup er for gammel",
                    "message": f"Seneste backup er {round(age.total_seconds() / 3600)} timer gammel.",
                }
            )

    channels = configured_channels(current_app.config)
    for finding in findings:
        created = append_event(
            finding["key"],
            finding["severity"],
            finding["title"],
            finding["message"],
            database_factory,
        )
        if created:
            enqueue_finding_notifications(
                finding,
                database_factory,
                channels,
                minimum_severity=current_app.config["NOTIFICATION_MIN_SEVERITY"],
            )

    return findings


def collect_snapshot(database_factory, *, include_usage=True):
    containers, docker_error = docker_status(include_usage=include_usage)
    metrics = system_metrics()
    backup = newest_backup()
    record_metrics(metrics, database_factory)
    findings = analyze_system(
        metrics,
        containers,
        backup,
        database_factory,
        docker_error=docker_error,
    )
    return containers, docker_error, metrics, backup, findings
