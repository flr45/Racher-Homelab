import os
from datetime import datetime
from pathlib import Path

import docker
import psutil
from flask import Flask, jsonify, render_template

app = Flask(__name__)

APP_LINKS = [
    {"name": "Vagtbytte", "url": os.getenv("VAGTBYTTE_URL", "#"), "icon": "🚒"},
    {"name": "Indsatsbrief", "url": os.getenv("INDSATSBRIEF_URL", "#"), "icon": "🚑"},
    {"name": "Minutregnskab", "url": os.getenv("MINUTREGNSKAB_URL", "#"), "icon": "⏱️"},
    {"name": "Portainer", "url": os.getenv("PORTAINER_URL", "#"), "icon": "🐳"},
    {"name": "Uptime Kuma", "url": os.getenv("UPTIME_KUMA_URL", "#"), "icon": "📈"},
    {"name": "Nginx Proxy Manager", "url": os.getenv("NPM_URL", "#"), "icon": "🌐"},
]

BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/backups"))


def docker_status():
    try:
        client = docker.from_env()
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


def newest_backup():
    try:
        candidates = [path for path in BACKUP_ROOT.iterdir() if path.is_dir()]
        if not candidates:
            return None
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        timestamp = datetime.fromtimestamp(newest.stat().st_mtime)
        return {"name": newest.name, "time": timestamp.strftime("%d-%m-%Y %H:%M")}
    except Exception:
        return None


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
            "docker_error": docker_error,
            "backup": newest_backup(),
            "updated": datetime.now().isoformat(),
        }
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})
