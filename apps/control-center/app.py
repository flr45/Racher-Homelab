import os
from datetime import datetime
from pathlib import Path

import docker
import psutil
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

APP_LINKS = [
    {"name": "Vagtbytte", "url": os.getenv("VAGTBYTTE_URL", "#"), "icon": "🚒"},
    {"name": "Indsatsbrief", "url": os.getenv("INDSATSBRIEF_URL", "#"), "icon": "🚑"},
    {"name": "Minutregnskab", "url": os.getenv("MINUTREGNSKAB_URL", "#"), "icon": "⏱️"},
    {"name": "Portainer", "url": os.getenv("PORTAINER_URL", "#"), "icon": "🐳"},
    {"name": "Uptime Kuma", "url": os.getenv("UPTIME_KUMA_URL", "#"), "icon": "📈"},
    {"name": "Nginx Proxy Manager", "url": os.getenv("NPM_URL", "#"), "icon": "🌐"},
]

DOMAIN_LINKS = [
    {"name": "Racher OS", "host": os.getenv("RACHER_OS_HOST", "home.racher.dk"), "service": "control-center"},
    {"name": "Vagtbytte", "host": os.getenv("VAGTBYTTE_HOST", "vagtbytte.racher.dk"), "service": "vagtbytte"},
    {"name": "Indsatsbrief", "host": os.getenv("INDSATSBRIEF_HOST", "indsatsbrief.racher.dk"), "service": "indsatsbrief"},
    {"name": "Minutregnskab", "host": os.getenv("MINUTREGNSKAB_HOST", "minutregnskab.racher.dk"), "service": "minutregnskab"},
]

BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/backups"))


def docker_client():
    return docker.from_env()


def docker_status():
    try:
        client = docker_client()
        containers = []
        for container in client.containers.list(all=True):
            state = container.attrs.get("State", {})
            containers.append(
                {
                    "name": container.name,
                    "status": container.status,
                    "healthy": state.get("Health", {}).get("Status"),
                    "image": container.image.tags[0] if container.image.tags else container.image.short_id,
                }
            )
        containers.sort(key=lambda item: item["name"])
        return containers, None
    except Exception as exc:
        return [], str(exc)


def domain_status(containers):
    states = {container["name"]: container["status"] for container in containers}
    result = []
    for domain in DOMAIN_LINKS:
        state = states.get(domain["service"], "not-found")
        result.append({**domain, "status": state, "url": f"https://{domain['host']}"})
    return result


def backups(limit=10):
    try:
        candidates = [path for path in BACKUP_ROOT.iterdir() if path.is_dir()]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [
            {
                "name": path.name,
                "time": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d-%m-%Y %H:%M"),
            }
            for path in candidates[:limit]
        ]
    except Exception:
        return []


def newest_backup():
    items = backups(limit=1)
    return items[0] if items else None


def system_metrics():
    disk = psutil.disk_usage("/")
    return {
        "cpu": round(psutil.cpu_percent(interval=0.2), 1),
        "ram": round(psutil.virtual_memory().percent, 1),
        "disk": round(disk.percent, 1),
        "temperature": read_temperature(),
        "uptime": format_uptime(),
    }


def read_temperature():
    paths = [
        Path("/host-sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/thermal/thermal_zone0/temp"),
    ]
    for path in paths:
        try:
            return round(float(path.read_text().strip()) / 1000, 1)
        except Exception:
            continue
    return None


def format_uptime():
    seconds = int(datetime.now().timestamp() - psutil.boot_time())
    days, remainder = divmod(seconds, 86400)
    hours, minutes = divmod(remainder, 3600)
    return f"{days}d {hours}t {minutes // 60}m"


@app.get("/")
def index():
    containers, docker_error = docker_status()
    return render_template(
        "index.html",
        links=APP_LINKS,
        domains=domain_status(containers),
        containers=containers,
        docker_error=docker_error,
        metrics=system_metrics(),
        backup=newest_backup(),
        updated=datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
    )


@app.get("/api/status")
def api_status():
    containers, docker_error = docker_status()
    return jsonify(
        {
            "metrics": system_metrics(),
            "containers": containers,
            "domains": domain_status(containers),
            "docker_error": docker_error,
            "backup": newest_backup(),
            "updated": datetime.now().isoformat(),
        }
    )


@app.get("/api/backups")
def api_backups():
    return jsonify({"backups": backups()})


@app.get("/api/containers/<container_name>/logs")
def api_container_logs(container_name):
    try:
        tail = min(max(request.args.get("tail", default=100, type=int), 1), 200)
        container = docker_client().containers.get(container_name)
        logs = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        return jsonify({"container": container.name, "tail": tail, "logs": logs})
    except docker.errors.NotFound:
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/health")
def health():
    return jsonify({"status": "ok"})
