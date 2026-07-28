import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Flask, current_app, jsonify, render_template, request, session

from config import Config
from services.audit_service import append_audit_entry, list_audit_entries
from services.database_service import open_database
from services.event_service import append_event, list_events
from services.backup_service import backups, newest_backup
from services.docker_service import (
    ContainerNotFoundError,
    app_status,
    container_logs,
    docker_status,
    domain_status,
    perform_container_action,
)
from services.metrics_service import (
    metric_history as load_metric_history,
)
from services.metrics_service import (
    record_metrics as store_metrics,
)
from services.metrics_service import (
    system_metrics,
)

blueprint = Blueprint("control_center", __name__)


def current_user():
    return request.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()


def admin_allowed():
    user = current_user()
    enabled = current_app.config["ADMIN_ACTIONS_ENABLED"]
    allowed_emails = current_app.config["ALLOWED_EMAILS"]
    return enabled and bool(user) and (not allowed_emails or user in allowed_emails)


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def database():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def record_metrics(metrics):
    store_metrics(metrics, database)


def metric_history(hours=24):
    return load_metric_history(hours, database)


def write_audit(action, target, success, message=""):
    append_audit_entry(
        action,
        target,
        success,
        message,
        current_user() or "unknown",
        database,
    )


def audit_history(limit=50):
    return list_audit_entries(limit, database)


def write_event(event_key, severity, title, message):
    return append_event(
        event_key,
        severity,
        title,
        message,
        database,
    )


def event_history(limit=50):
    return list_events(limit, database)


def analyze_system(metrics, containers, backup):
    findings = []
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
    for finding in findings:
        write_event(
            finding["key"],
            finding["severity"],
            finding["title"],
            finding["message"],
        )
    return findings


def assistant_answer(question, metrics, containers, backup, findings):
    q = question.lower().strip()
    stopped = [c["name"] for c in containers if c["status"] != "running"]
    unhealthy = [c["name"] for c in containers if c.get("healthy") == "unhealthy"]
    if any(word in q for word in ["fejl", "problem", "usædvan", "status", "sund"]):
        if not findings:
            return "Jeg kan ikke se aktuelle advarsler. Alle fundne containere kører, og systemmålingerne er under de konfigurerede grænser."
        return "Aktuelle fund: " + " ".join(
            f"{finding['title']}: {finding['message']}" for finding in findings
        )
    if "backup" in q:
        return (
            f"Seneste backup er {backup['name']} fra {backup['time']} og fylder {backup['size_mb']} MB."
            if backup
            else "Der er ikke fundet nogen backup i backupmappen."
        )
    if "container" in q or "docker" in q:
        if stopped or unhealthy:
            return f"Stoppede containere: {', '.join(stopped) or 'ingen'}. Unhealthy containere: {', '.join(unhealthy) or 'ingen'}."
        return f"Alle {len(containers)} fundne containere kører uden registreret unhealthy-status."
    if "ram" in q:
        top = sorted(
            (c for c in containers if c.get("memory_mb") is not None),
            key=lambda c: c["memory_mb"],
            reverse=True,
        )[:3]
        detail = ", ".join(f"{c['name']} {c['memory_mb']} MB" for c in top) or "ingen containerdata"
        return f"Systemets RAM-forbrug er {metrics['ram']}%. Største containere lige nu: {detail}."
    if "cpu" in q:
        top = sorted(
            (c for c in containers if c.get("cpu") is not None),
            key=lambda c: c["cpu"],
            reverse=True,
        )[:3]
        detail = ", ".join(f"{c['name']} {c['cpu']}%" for c in top) or "ingen containerdata"
        return f"Systemets CPU-forbrug er {metrics['cpu']}%. Største containere lige nu: {detail}."
    if "temperatur" in q or "varm" in q:
        return (
            f"Den registrerede temperatur er {metrics['temperature']}°C."
            if metrics["temperature"] is not None
            else "Temperaturen kan ikke aflæses på denne installation."
        )
    return f"Systemet bruger CPU {metrics['cpu']}%, RAM {metrics['ram']}% og SSD {metrics['disk']}%. Jeg har registreret {len(findings)} aktuelle advarsler. Prøv fx: 'Vis fejl', 'Hvordan ser backup ud?' eller 'Hvad bruger mest RAM?'"


def snapshot():
    containers, docker_error = docker_status()
    metrics = system_metrics()
    backup = newest_backup()
    record_metrics(metrics)
    findings = analyze_system(metrics, containers, backup)
    return containers, docker_error, metrics, backup, findings


@blueprint.get("/")
def index():
    containers, docker_error, metrics, backup, findings = snapshot()
    return render_template(
        "index.html",
        apps=app_status(containers),
        domains=domain_status(containers),
        containers=containers,
        docker_error=docker_error,
        metrics=metrics,
        backup=backup,
        findings=findings,
        events=event_history(10),
        admin_enabled=admin_allowed(),
        csrf_token=csrf_token(),
        audit=audit_history(10),
        updated=datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
    )


@blueprint.get("/api/status")
def api_status():
    containers, docker_error, metrics, backup, findings = snapshot()
    return jsonify(
        {
            "metrics": metrics,
            "containers": containers,
            "apps": app_status(containers),
            "domains": domain_status(containers),
            "docker_error": docker_error,
            "backup": backup,
            "findings": findings,
            "admin_enabled": admin_allowed(),
            "updated": datetime.now().isoformat(),
        }
    )


@blueprint.get("/api/history")
def api_history():
    hours = min(max(request.args.get("hours", default=24, type=int), 1), 24 * 30)
    return jsonify({"hours": hours, "points": metric_history(hours)})


@blueprint.get("/api/backups")
def api_backups():
    return jsonify({"backups": backups()})


@blueprint.get("/api/audit")
def api_audit():
    limit = min(max(request.args.get("limit", default=50, type=int), 1), 200)
    return jsonify({"events": audit_history(limit)})


@blueprint.get("/api/events")
def api_events():
    limit = min(max(request.args.get("limit", default=50, type=int), 1), 200)
    return jsonify({"events": event_history(limit)})


@blueprint.post("/api/assistant")
def api_assistant():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", ""))[:500]
    if not question.strip():
        return jsonify({"error": "Skriv et spørgsmål."}), 400
    containers, _, metrics, backup, findings = snapshot()
    return jsonify(
        {
            "answer": assistant_answer(
                question, metrics, containers, backup, findings
            ),
            "findings": findings,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@blueprint.get("/api/containers/<container_name>/logs")
def api_container_logs(container_name):
    try:
        tail = min(max(request.args.get("tail", default=100, type=int), 1), 500)
        resolved_name, logs = container_logs(container_name, tail)
        return jsonify({"container": resolved_name, "tail": tail, "logs": logs})
    except ContainerNotFoundError:
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@blueprint.post("/api/containers/<container_name>/<action>")
def api_container_action(container_name, action):
    if not admin_allowed():
        return (
            jsonify(
                {
                    "error": "Administrative handlinger er ikke aktiveret eller brugeren er ikke godkendt."
                }
            ),
            403,
        )
    if request.headers.get("X-CSRF-Token") != session.get("csrf_token"):
        return jsonify({"error": "Ugyldig sikkerhedstoken."}), 403
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "Ukendt handling."}), 400
    protected_containers = current_app.config["PROTECTED_CONTAINERS"]
    if container_name in protected_containers and action in {"stop", "restart"}:
        write_audit(action, container_name, False, "Beskyttet container")
        return jsonify({"error": "Containeren er beskyttet mod denne handling."}), 409
    try:
        perform_container_action(container_name, action)
        write_audit(action, container_name, True, "Udført")
        return jsonify({"ok": True, "container": container_name, "action": action})
    except ContainerNotFoundError:
        write_audit(action, container_name, False, "Ikke fundet")
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    except Exception as exc:
        write_audit(action, container_name, False, str(exc))
        return jsonify({"error": str(exc)}), 503


@blueprint.get("/health")
def health():
    return jsonify({"status": "ok"})


def create_app(config=None):
    flask_app = Flask(__name__)
    flask_app.config.from_object(Config)
    if config:
        flask_app.config.update(config)
    if not flask_app.config.get("SECRET_KEY"):
        flask_app.config["SECRET_KEY"] = secrets.token_hex(32)
    flask_app.register_blueprint(blueprint)
    return flask_app


app = create_app()
