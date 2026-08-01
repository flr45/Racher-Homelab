import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import docker
from flask import Blueprint, current_app, jsonify, make_response, render_template

from backup_verification_extension import build_backup_verification_report
from rbac_extension import current_identity
from services import module_registry_service
from services.docker_service import docker_client, docker_status
from services.rbac_service import has_permission

operations_status_blueprint = Blueprint("operations_status", __name__)

OPERATIONS_STATUS_MODULE = {
    "id": "operations-status",
    "name": "Driftsstatus",
    "description": "Samlet live-status for modem, SMS-kø, Vagtbytte, backups, Cloudflare og containere.",
    "href": "/operations-status",
    "category": "overview",
    "permission": "system.read",
    "status_endpoint": "/api/operations-status",
}

DEFAULT_REQUIRED_CONTAINERS = (
    "nginx-proxy-manager",
    "control-center",
    "portainer",
    "uptime-kuma",
    "npm-db",
    "postgres",
    "redis",
    "vagtbytte-web",
    "vagtbytte-worker",
    "racher-sms-gateway",
    "minutregnskab",
    "cloudflared",
)

STATE_RANK = {"healthy": 0, "warning": 1, "critical": 2, "unknown": 1}


def _setting(name, default):
    return current_app.config.get(name, os.getenv(name, default))


def _integer_setting(name, default, minimum=1, maximum=86400):
    try:
        value = int(_setting(name, default))
    except (TypeError, ValueError):
        value = int(default)
    return min(maximum, max(minimum, value))


def _required_containers():
    configured = _setting(
        "OPERATIONS_STATUS_CONTAINERS",
        ",".join(DEFAULT_REQUIRED_CONTAINERS),
    )
    if isinstance(configured, (tuple, list, set)):
        values = [str(value).strip() for value in configured]
    else:
        values = [value.strip() for value in str(configured).split(",")]
    return tuple(dict.fromkeys(value for value in values if value))


def fetch_json(url, timeout=None):
    timeout = timeout or _integer_setting(
        "OPERATIONS_STATUS_HTTP_TIMEOUT_SECONDS", 6, minimum=1, maximum=30
    )
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Racher-OS-Control-Center/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            return {
                "available": True,
                "status_code": response.status,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "payload": payload if isinstance(payload, dict) else {},
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        return {
            "available": False,
            "status_code": exc.code,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "payload": {},
            "error": f"HTTP {exc.code}",
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "available": False,
            "status_code": None,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "payload": {},
            "error": str(exc)[:300],
        }


def _container_state(container):
    if container is None:
        return "critical"
    if container.get("status") != "running":
        return "critical"
    health = str(container.get("healthy") or "none").lower()
    if health == "unhealthy":
        return "critical"
    if health == "starting":
        return "warning"
    return "healthy"


def _container_metric(container):
    if container is None:
        return {"status": "missing", "health": "unknown", "state": "critical"}
    return {
        "status": container.get("status") or "unknown",
        "health": container.get("healthy") or "none",
        "state": _container_state(container),
    }


def collect_vagtbytte_backup(client=None):
    max_age = _integer_setting(
        "VAGTBYTTE_BACKUP_MAX_AGE_HOURS", 30, minimum=1, maximum=24 * 30
    )
    container_name = str(_setting("VAGTBYTTE_CONTAINER_NAME", "vagtbytte-web"))
    command = (
        "latest=$(ls -1t /data/backups/*.vagtbackup.enc 2>/dev/null | head -1); "
        "test -n \"$latest\" || exit 2; "
        "printf '%s|%s\\n' \"$(stat -c %Y \"$latest\")\" \"$(basename \"$latest\")\""
    )

    try:
        active_client = client or docker_client()
        container = active_client.containers.get(container_name)
        result = container.exec_run(["sh", "-lc", command])
        if hasattr(result, "exit_code"):
            exit_code = result.exit_code
            output = result.output
        else:
            exit_code, output = result
        text = output.decode("utf-8", errors="replace").strip()
        if exit_code == 2:
            return {
                "status": "missing",
                "state": "critical",
                "latest": None,
                "age_hours": None,
                "max_age_hours": max_age,
                "error": None,
            }
        if exit_code != 0 or "|" not in text:
            raise RuntimeError(text or f"docker exec sluttede med kode {exit_code}")
        epoch_text, name = text.split("|", 1)
        recorded_at = datetime.fromtimestamp(float(epoch_text), tz=timezone.utc)
        age_hours = max(
            0.0,
            round(
                (datetime.now(timezone.utc) - recorded_at).total_seconds() / 3600,
                1,
            ),
        )
        stale = age_hours > max_age
        return {
            "status": "stale" if stale else "verified",
            "state": "warning" if stale else "healthy",
            "latest": name,
            "recorded_at": recorded_at.isoformat(),
            "age_hours": age_hours,
            "max_age_hours": max_age,
            "error": None,
        }
    except docker.errors.NotFound:
        return {
            "status": "unavailable",
            "state": "critical",
            "latest": None,
            "age_hours": None,
            "max_age_hours": max_age,
            "error": f"Containeren {container_name} blev ikke fundet",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unavailable",
            "state": "critical",
            "latest": None,
            "age_hours": None,
            "max_age_hours": max_age,
            "error": str(exc)[:300],
        }


def _card(identifier, title, description, state, metrics, details=None):
    return {
        "id": identifier,
        "title": title,
        "description": description,
        "state": state,
        "metrics": metrics,
        "details": details or [],
    }


def _sms_card(result, container):
    payload = result.get("payload", {})
    gateway = payload.get("gateway", {}) if isinstance(payload, dict) else {}
    modem = payload.get("modem", {}) if isinstance(payload, dict) else {}
    pending = int(gateway.get("outbox_pending", 0) or 0)
    failed = int(gateway.get("outbox_failed", 0) or 0)
    container_state = _container_state(container)

    state = container_state
    if not result.get("available"):
        state = "critical"
    elif str(payload.get("status", "unknown")).lower() != "ok":
        state = "critical"
    elif str(modem.get("state", "unknown")).lower() != "online":
        state = "critical"
    elif str(gateway.get("database", "unknown")).lower() != "online":
        state = "critical"
    elif pending or failed:
        state = "warning"

    details = []
    if result.get("error"):
        details.append(result["error"])
    if gateway.get("last_received_sms_at"):
        details.append(f"Senest modtaget: {gateway['last_received_sms_at']}")
    if gateway.get("last_error"):
        details.append(str(gateway["last_error"])[:300])

    return _card(
        "sms-gateway",
        "SMS-gateway",
        "Huawei-modem, gatewaydatabase og vedvarende SMS-udbakke.",
        state,
        [
            {"label": "Modem", "value": modem.get("state", "ukendt")},
            {"label": "Database", "value": gateway.get("database", "ukendt")},
            {"label": "I kø", "value": pending},
            {"label": "Fejlet", "value": failed},
            {"label": "Svartid", "value": f"{result.get('latency_ms', 0)} ms"},
            {"label": "Container", "value": _container_metric(container)["status"]},
        ],
        details,
    )


def _vagtbytte_card(result, container):
    payload = result.get("payload", {})
    raw_state = str(payload.get("status") or payload.get("state") or "ok").lower()
    healthy_http_states = {"ok", "healthy", "online", "ready"}
    state = _container_state(container)
    if not result.get("available"):
        state = "critical"
    elif raw_state not in healthy_http_states:
        state = "warning"

    database = payload.get("database")
    if isinstance(database, dict):
        database = database.get("status") or database.get("state")

    details = []
    if result.get("error"):
        details.append(result["error"])
    message = payload.get("message")
    if message:
        details.append(str(message)[:300])

    return _card(
        "vagtbytte",
        "Vagtbytte",
        "Webapp, API-health og intern forbindelse til alarmkæden.",
        state,
        [
            {"label": "API", "value": raw_state},
            {"label": "Database", "value": database or "ikke oplyst"},
            {"label": "Svartid", "value": f"{result.get('latency_ms', 0)} ms"},
            {"label": "Container", "value": _container_metric(container)["status"]},
            {"label": "Health", "value": _container_metric(container)["health"]},
            {"label": "HTTP", "value": result.get("status_code") or "–"},
        ],
        details,
    )


def _backup_card(homelab, vagtbytte):
    homelab_status = str(homelab.get("status", "unknown"))
    if homelab_status == "verified":
        homelab_state = "healthy"
    elif homelab_status == "stale":
        homelab_state = "warning"
    else:
        homelab_state = "critical"

    state = max(
        (homelab_state, vagtbytte.get("state", "critical")),
        key=lambda value: STATE_RANK.get(value, 1),
    )
    details = []
    validation = homelab.get("validation") or {}
    details.extend(str(item) for item in validation.get("errors", [])[:3])
    if vagtbytte.get("error"):
        details.append(vagtbytte["error"])

    return _card(
        "backups",
        "Backups",
        "Verificeret Homelab-backup og krypteret Vagtbytte-backup.",
        state,
        [
            {"label": "Homelab", "value": homelab_status},
            {
                "label": "Alder",
                "value": f"{homelab.get('age_hours')} t"
                if homelab.get("age_hours") is not None
                else "–",
            },
            {"label": "Vagtbytte", "value": vagtbytte.get("status", "ukendt")},
            {
                "label": "Alder",
                "value": f"{vagtbytte.get('age_hours')} t"
                if vagtbytte.get("age_hours") is not None
                else "–",
            },
            {"label": "Maks.", "value": f"{homelab.get('max_age_hours', '–')} t"},
            {"label": "Krypteret", "value": "ja" if vagtbytte.get("latest") else "ukendt"},
        ],
        details,
    )


def _cloudflare_card(container):
    metric = _container_metric(container)
    return _card(
        "cloudflare",
        "Cloudflare Tunnel",
        "Tunnelcontaineren, som publicerer de valgte Racher OS-tjenester.",
        metric["state"],
        [
            {"label": "Status", "value": metric["status"]},
            {"label": "Health", "value": metric["health"]},
            {"label": "Container", "value": "cloudflared"},
            {
                "label": "Beskyttet",
                "value": "ja" if container and container.get("protected") else "nej",
            },
        ],
    )


def _containers_card(container_map, docker_error):
    rows = []
    critical = 0
    warning = 0
    for name in _required_containers():
        container = container_map.get(name)
        metric = _container_metric(container)
        rows.append(
            {
                "name": name,
                "status": metric["status"],
                "health": metric["health"],
                "state": metric["state"],
            }
        )
        if metric["state"] == "critical":
            critical += 1
        elif metric["state"] == "warning":
            warning += 1

    state = "critical" if docker_error or critical else "warning" if warning else "healthy"
    return _card(
        "containers",
        "Docker & containere",
        "De centrale containere, der skal være tilgængelige for normal drift.",
        state,
        [
            {"label": "Kontrolleret", "value": len(rows)},
            {"label": "OK", "value": len(rows) - critical - warning},
            {"label": "Advarsel", "value": warning},
            {"label": "Fejl", "value": critical},
        ],
        [docker_error] if docker_error else [],
    ), rows


def build_operations_status_report():
    containers, docker_error = docker_status(include_usage=False)
    container_map = {item["name"]: item for item in containers}

    sms_result = fetch_json(
        str(_setting("SMS_GATEWAY_HEALTH_URL", "http://sms-gateway:8080/health"))
    )
    vagtbytte_result = fetch_json(
        str(_setting("VAGTBYTTE_HEALTH_URL", "http://vagtbytte-web:3000/api/health"))
    )
    homelab_backup = build_backup_verification_report()
    vagtbytte_backup = collect_vagtbytte_backup()
    containers_card, required_rows = _containers_card(container_map, docker_error)

    cards = [
        _sms_card(sms_result, container_map.get("racher-sms-gateway")),
        _vagtbytte_card(vagtbytte_result, container_map.get("vagtbytte-web")),
        _backup_card(homelab_backup, vagtbytte_backup),
        _cloudflare_card(container_map.get("cloudflared")),
        containers_card,
    ]

    counts = {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
    for card in cards:
        counts[card["state"]] = counts.get(card["state"], 0) + 1
    overall = (
        "critical"
        if counts["critical"]
        else "warning"
        if counts["warning"]
        else "healthy"
    )

    return {
        "summary": {
            "state": overall,
            "healthy": counts["healthy"],
            "warning": counts["warning"],
            "critical": counts["critical"],
            "total": len(cards),
        },
        "cards": cards,
        "containers": required_rows,
        "updated_at": datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
        "read_only": True,
    }


def _can_read(identity):
    return has_permission(identity["role"], "system.read")


@operations_status_blueprint.get("/operations-status")
def operations_status_page():
    identity = current_identity()
    if not _can_read(identity):
        return jsonify({"error": "Brugeren har ikke adgang til Driftsstatus."}), 403
    response = make_response(
        render_template(
            "operations_status.html",
            report=build_operations_status_report(),
            actor=identity.get("email") or identity["role"],
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@operations_status_blueprint.get("/api/operations-status")
def operations_status_api():
    identity = current_identity()
    if not _can_read(identity):
        return jsonify({"error": "Brugeren har ikke adgang til Driftsstatus."}), 403
    payload = build_operations_status_report()
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def _register_navigation_module():
    modules = list(module_registry_service.MODULES)
    if any(item["id"] == OPERATIONS_STATUS_MODULE["id"] for item in modules):
        return
    insert_at = 1
    for index, module in enumerate(modules):
        if module["id"] == "app-center":
            insert_at = index + 1
            break
        if module["id"] == "dashboard":
            insert_at = index + 1
    modules.insert(insert_at, dict(OPERATIONS_STATUS_MODULE))
    module_registry_service.MODULES = tuple(modules)


def init_operations_status_center(app):
    _register_navigation_module()
    app.register_blueprint(operations_status_blueprint)
