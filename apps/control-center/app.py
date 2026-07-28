import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import docker
import psutil
from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = os.getenv("RACHER_OS_SECRET_KEY", secrets.token_hex(32))

APP_LINKS = [
    {"name": "Vagtbytte", "url": os.getenv("VAGTBYTTE_URL", "#"), "icon": "🚒", "service": "vagtbytte", "version": os.getenv("VAGTBYTTE_VERSION", "–")},
    {"name": "Indsatsbrief", "url": os.getenv("INDSATSBRIEF_URL", "#"), "icon": "🚑", "service": "indsatsbrief", "version": os.getenv("INDSATSBRIEF_VERSION", "–")},
    {"name": "Minutregnskab", "url": os.getenv("MINUTREGNSKAB_URL", "#"), "icon": "⏱️", "service": "minutregnskab", "version": os.getenv("MINUTREGNSKAB_VERSION", "–")},
    {"name": "Portainer", "url": os.getenv("PORTAINER_URL", "#"), "icon": "🐳", "service": "portainer", "version": "infra"},
    {"name": "Uptime Kuma", "url": os.getenv("UPTIME_KUMA_URL", "#"), "icon": "📈", "service": "uptime-kuma", "version": "infra"},
    {"name": "Nginx Proxy Manager", "url": os.getenv("NPM_URL", "#"), "icon": "🌐", "service": "nginx-proxy-manager", "version": "infra"},
]

DOMAIN_LINKS = [
    {"name": "Racher OS", "host": os.getenv("RACHER_OS_HOST", "home.racher.dk"), "service": "control-center"},
    {"name": "Vagtbytte", "host": os.getenv("VAGTBYTTE_HOST", "vagtbytte.racher.dk"), "service": "vagtbytte"},
    {"name": "Indsatsbrief", "host": os.getenv("INDSATSBRIEF_HOST", "indsatsbrief.racher.dk"), "service": "indsatsbrief"},
    {"name": "Minutregnskab", "host": os.getenv("MINUTREGNSKAB_HOST", "minutregnskab.racher.dk"), "service": "minutregnskab"},
]

BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/backups"))
DATA_ROOT = Path(os.getenv("RACHER_OS_DATA", "/data"))
DATABASE_PATH = DATA_ROOT / "racher-os.db"
ADMIN_ACTIONS_ENABLED = os.getenv("ADMIN_ACTIONS_ENABLED", "false").lower() == "true"
ALLOWED_EMAILS = {v.strip().lower() for v in os.getenv("ALLOWED_ADMIN_EMAILS", "").split(",") if v.strip()}
PROTECTED_CONTAINERS = {v.strip() for v in os.getenv("PROTECTED_CONTAINERS", "control-center,cloudflared").split(",") if v.strip()}
CPU_WARNING = float(os.getenv("CPU_WARNING_PERCENT", "85"))
RAM_WARNING = float(os.getenv("RAM_WARNING_PERCENT", "85"))
DISK_WARNING = float(os.getenv("DISK_WARNING_PERCENT", "85"))
TEMP_WARNING = float(os.getenv("TEMP_WARNING_C", "75"))
BACKUP_MAX_AGE_HOURS = int(os.getenv("BACKUP_MAX_AGE_HOURS", "36"))


def docker_client():
    return docker.from_env()


def current_user():
    return request.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()


def admin_allowed():
    user = current_user()
    return ADMIN_ACTIONS_ENABLED and bool(user) and (not ALLOWED_EMAILS or user in ALLOWED_EMAILS)


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def container_usage(container):
    try:
        stats = container.stats(stream=False)
        cpu_stats = stats.get("cpu_stats", {})
        pre_cpu = stats.get("precpu_stats", {})
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - pre_cpu.get("cpu_usage", {}).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - pre_cpu.get("system_cpu_usage", 0)
        cpu_count = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1
        cpu_percent = (cpu_delta / system_delta * cpu_count * 100) if system_delta > 0 and cpu_delta >= 0 else 0
        memory = stats.get("memory_stats", {}).get("usage", 0)
        cache = stats.get("memory_stats", {}).get("stats", {}).get("cache", 0)
        return {"cpu": round(cpu_percent, 1), "memory_mb": round(max(memory - cache, 0) / 1024 / 1024, 1)}
    except Exception:
        return {"cpu": None, "memory_mb": None}


def docker_status(include_usage=True):
    try:
        containers = []
        for container in docker_client().containers.list(all=True):
            state = container.attrs.get("State", {})
            usage = container_usage(container) if include_usage and container.status == "running" else {"cpu": None, "memory_mb": None}
            containers.append({
                "name": container.name,
                "status": container.status,
                "healthy": state.get("Health", {}).get("Status"),
                "image": container.image.tags[0] if container.image.tags else container.image.short_id,
                "started_at": state.get("StartedAt"),
                "protected": container.name in PROTECTED_CONTAINERS,
                **usage,
            })
        containers.sort(key=lambda item: item["name"])
        return containers, None
    except Exception as exc:
        return [], str(exc)


def app_status(containers):
    states = {container["name"]: container for container in containers}
    return [{**item, "container": states.get(item["service"]), "status": states.get(item["service"], {}).get("status", "not-found")} for item in APP_LINKS]


def domain_status(containers):
    states = {container["name"]: container["status"] for container in containers}
    return [{**domain, "status": states.get(domain["service"], "not-found"), "url": f"https://{domain['host']}"} for domain in DOMAIN_LINKS]


def directory_size(path):
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except Exception:
        return 0


def backups(limit=20):
    try:
        candidates = [path for path in BACKUP_ROOT.iterdir() if path.is_dir()]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [{
            "name": path.name,
            "time": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d-%m-%Y %H:%M"),
            "recorded_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "size_mb": round(directory_size(path) / 1024 / 1024, 1),
        } for path in candidates[:limit]]
    except Exception:
        return []


def newest_backup():
    items = backups(limit=1)
    return items[0] if items else None


def system_metrics():
    disk = psutil.disk_usage("/")
    network = psutil.net_io_counters()
    return {
        "cpu": round(psutil.cpu_percent(interval=0.2), 1),
        "ram": round(psutil.virtual_memory().percent, 1),
        "disk": round(disk.percent, 1),
        "temperature": read_temperature(),
        "uptime": format_uptime(),
        "network_sent_mb": round(network.bytes_sent / 1024 / 1024, 1),
        "network_recv_mb": round(network.bytes_recv / 1024 / 1024, 1),
    }


def read_temperature():
    for path in [Path("/host-sys/class/thermal/thermal_zone0/temp"), Path("/sys/class/thermal/thermal_zone0/temp")]:
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


def database():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS metrics (recorded_at TEXT PRIMARY KEY, cpu REAL NOT NULL, ram REAL NOT NULL, disk REAL NOT NULL, temperature REAL, network_sent_mb REAL NOT NULL, network_recv_mb REAL NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, success INTEGER NOT NULL, message TEXT)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, event_key TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL)""")
    return connection


def record_metrics(metrics):
    now = datetime.now(timezone.utc)
    with database() as connection:
        latest = connection.execute("SELECT recorded_at FROM metrics ORDER BY recorded_at DESC LIMIT 1").fetchone()
        if latest and now - datetime.fromisoformat(latest["recorded_at"]) < timedelta(seconds=25):
            return
        connection.execute("INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?)", (now.isoformat(), metrics["cpu"], metrics["ram"], metrics["disk"], metrics["temperature"], metrics["network_sent_mb"], metrics["network_recv_mb"]))
        connection.execute("DELETE FROM metrics WHERE recorded_at < ?", ((now - timedelta(days=30)).isoformat(),))


def metric_history(hours=24):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with database() as connection:
        rows = connection.execute("SELECT * FROM metrics WHERE recorded_at >= ? ORDER BY recorded_at", (since.isoformat(),)).fetchall()
    return [dict(row) for row in rows]


def write_audit(action, target, success, message=""):
    with database() as connection:
        connection.execute("INSERT INTO audit_log (recorded_at, actor, action, target, success, message) VALUES (?, ?, ?, ?, ?, ?)", (datetime.now(timezone.utc).isoformat(), current_user() or "unknown", action, target, int(success), message[:500]))


def audit_history(limit=50):
    with database() as connection:
        rows = connection.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def write_event(event_key, severity, title, message):
    now = datetime.now(timezone.utc)
    with database() as connection:
        latest = connection.execute("SELECT recorded_at FROM events WHERE event_key = ? ORDER BY id DESC LIMIT 1", (event_key,)).fetchone()
        if latest and now - datetime.fromisoformat(latest["recorded_at"]) < timedelta(hours=1):
            return
        connection.execute("INSERT INTO events (recorded_at, event_key, severity, title, message) VALUES (?, ?, ?, ?, ?)", (now.isoformat(), event_key, severity, title, message[:500]))
        connection.execute("DELETE FROM events WHERE recorded_at < ?", ((now - timedelta(days=30)).isoformat(),))


def event_history(limit=50):
    with database() as connection:
        rows = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def analyze_system(metrics, containers, backup):
    findings = []
    for key, value, threshold, label in [
        ("cpu", metrics["cpu"], CPU_WARNING, "CPU"),
        ("ram", metrics["ram"], RAM_WARNING, "RAM"),
        ("disk", metrics["disk"], DISK_WARNING, "SSD"),
    ]:
        if value >= threshold:
            findings.append({"key": f"metric:{key}", "severity": "warning", "title": f"Høj {label}-belastning", "message": f"{label} er på {value}% (grænse {threshold}%)."})
    if metrics["temperature"] is not None and metrics["temperature"] >= TEMP_WARNING:
        findings.append({"key": "metric:temperature", "severity": "warning", "title": "Høj temperatur", "message": f"Servertemperaturen er {metrics['temperature']}°C."})
    for container in containers:
        if container["status"] != "running":
            findings.append({"key": f"container:{container['name']}:stopped", "severity": "critical", "title": "Container stoppet", "message": f"{container['name']} har status {container['status']}."})
        elif container.get("healthy") == "unhealthy":
            findings.append({"key": f"container:{container['name']}:unhealthy", "severity": "critical", "title": "Container unhealthy", "message": f"{container['name']} fejler sit healthcheck."})
    if not backup:
        findings.append({"key": "backup:missing", "severity": "warning", "title": "Ingen backup fundet", "message": "Backupmappen indeholder ingen registreret backup."})
    else:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(backup["recorded_at"])
        if age > timedelta(hours=BACKUP_MAX_AGE_HOURS):
            findings.append({"key": "backup:old", "severity": "warning", "title": "Backup er for gammel", "message": f"Seneste backup er {round(age.total_seconds() / 3600)} timer gammel."})
    for finding in findings:
        write_event(finding["key"], finding["severity"], finding["title"], finding["message"])
    return findings


def assistant_answer(question, metrics, containers, backup, findings):
    q = question.lower().strip()
    stopped = [c["name"] for c in containers if c["status"] != "running"]
    unhealthy = [c["name"] for c in containers if c.get("healthy") == "unhealthy"]
    if any(word in q for word in ["fejl", "problem", "usædvan", "status", "sund"]):
        if not findings:
            return "Jeg kan ikke se aktuelle advarsler. Alle fundne containere kører, og systemmålingerne er under de konfigurerede grænser."
        return "Aktuelle fund: " + " ".join(f"{f['title']}: {f['message']}" for f in findings)
    if "backup" in q:
        return f"Seneste backup er {backup['name']} fra {backup['time']} og fylder {backup['size_mb']} MB." if backup else "Der er ikke fundet nogen backup i backupmappen."
    if "container" in q or "docker" in q:
        if stopped or unhealthy:
            return f"Stoppede containere: {', '.join(stopped) or 'ingen'}. Unhealthy containere: {', '.join(unhealthy) or 'ingen'}."
        return f"Alle {len(containers)} fundne containere kører uden registreret unhealthy-status."
    if "ram" in q:
        top = sorted((c for c in containers if c.get("memory_mb") is not None), key=lambda c: c["memory_mb"], reverse=True)[:3]
        detail = ", ".join(f"{c['name']} {c['memory_mb']} MB" for c in top) or "ingen containerdata"
        return f"Systemets RAM-forbrug er {metrics['ram']}%. Største containere lige nu: {detail}."
    if "cpu" in q:
        top = sorted((c for c in containers if c.get("cpu") is not None), key=lambda c: c["cpu"], reverse=True)[:3]
        detail = ", ".join(f"{c['name']} {c['cpu']}%" for c in top) or "ingen containerdata"
        return f"Systemets CPU-forbrug er {metrics['cpu']}%. Største containere lige nu: {detail}."
    if "temperatur" in q or "varm" in q:
        return f"Den registrerede temperatur er {metrics['temperature']}°C." if metrics["temperature"] is not None else "Temperaturen kan ikke aflæses på denne installation."
    return f"Systemet bruger CPU {metrics['cpu']}%, RAM {metrics['ram']}% og SSD {metrics['disk']}%. Jeg har registreret {len(findings)} aktuelle advarsler. Prøv fx: 'Vis fejl', 'Hvordan ser backup ud?' eller 'Hvad bruger mest RAM?'"


def snapshot():
    containers, docker_error = docker_status()
    metrics = system_metrics()
    backup = newest_backup()
    record_metrics(metrics)
    findings = analyze_system(metrics, containers, backup)
    return containers, docker_error, metrics, backup, findings


@app.get("/")
def index():
    containers, docker_error, metrics, backup, findings = snapshot()
    return render_template("index.html", apps=app_status(containers), domains=domain_status(containers), containers=containers, docker_error=docker_error, metrics=metrics, backup=backup, findings=findings, events=event_history(10), admin_enabled=admin_allowed(), csrf_token=csrf_token(), audit=audit_history(10), updated=datetime.now().strftime("%d-%m-%Y %H:%M:%S"))


@app.get("/api/status")
def api_status():
    containers, docker_error, metrics, backup, findings = snapshot()
    return jsonify({"metrics": metrics, "containers": containers, "apps": app_status(containers), "domains": domain_status(containers), "docker_error": docker_error, "backup": backup, "findings": findings, "admin_enabled": admin_allowed(), "updated": datetime.now().isoformat()})


@app.get("/api/history")
def api_history():
    hours = min(max(request.args.get("hours", default=24, type=int), 1), 24 * 30)
    return jsonify({"hours": hours, "points": metric_history(hours)})


@app.get("/api/backups")
def api_backups():
    return jsonify({"backups": backups()})


@app.get("/api/audit")
def api_audit():
    return jsonify({"events": audit_history(min(max(request.args.get("limit", default=50, type=int), 1), 200))})


@app.get("/api/events")
def api_events():
    return jsonify({"events": event_history(min(max(request.args.get("limit", default=50, type=int), 1), 200))})


@app.post("/api/assistant")
def api_assistant():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", ""))[:500]
    if not question.strip():
        return jsonify({"error": "Skriv et spørgsmål."}), 400
    containers, _, metrics, backup, findings = snapshot()
    return jsonify({"answer": assistant_answer(question, metrics, containers, backup, findings), "findings": findings, "generated_at": datetime.now(timezone.utc).isoformat()})


@app.get("/api/containers/<container_name>/logs")
def api_container_logs(container_name):
    try:
        tail = min(max(request.args.get("tail", default=100, type=int), 1), 500)
        container = docker_client().containers.get(container_name)
        logs = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        return jsonify({"container": container.name, "tail": tail, "logs": logs})
    except docker.errors.NotFound:
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.post("/api/containers/<container_name>/<action>")
def api_container_action(container_name, action):
    if not admin_allowed():
        return jsonify({"error": "Administrative handlinger er ikke aktiveret eller brugeren er ikke godkendt."}), 403
    if request.headers.get("X-CSRF-Token") != session.get("csrf_token"):
        return jsonify({"error": "Ugyldig sikkerhedstoken."}), 403
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "Ukendt handling."}), 400
    if container_name in PROTECTED_CONTAINERS and action in {"stop", "restart"}:
        write_audit(action, container_name, False, "Beskyttet container")
        return jsonify({"error": "Containeren er beskyttet mod denne handling."}), 409
    try:
        container = docker_client().containers.get(container_name)
        getattr(container, action)(timeout=20) if action in {"stop", "restart"} else container.start()
        write_audit(action, container_name, True, "Udført")
        return jsonify({"ok": True, "container": container_name, "action": action})
    except docker.errors.NotFound:
        write_audit(action, container_name, False, "Ikke fundet")
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    except Exception as exc:
        write_audit(action, container_name, False, str(exc))
        return jsonify({"error": str(exc)}), 503


@app.get("/health")
def health():
    return jsonify({"status": "ok"})
