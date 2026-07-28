from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil


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
    for path in [
        Path("/host-sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/thermal/thermal_zone0/temp"),
    ]:
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


def record_metrics(metrics, database_factory):
    now = datetime.now(timezone.utc)
    with database_factory() as connection:
        latest = connection.execute(
            "SELECT recorded_at FROM metrics ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        if latest and now - datetime.fromisoformat(latest["recorded_at"]) < timedelta(
            seconds=25
        ):
            return
        connection.execute(
            "INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(),
                metrics["cpu"],
                metrics["ram"],
                metrics["disk"],
                metrics["temperature"],
                metrics["network_sent_mb"],
                metrics["network_recv_mb"],
            ),
        )
        connection.execute(
            "DELETE FROM metrics WHERE recorded_at < ?",
            ((now - timedelta(days=30)).isoformat(),),
        )


def metric_history(hours, database_factory):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with database_factory() as connection:
        rows = connection.execute(
            "SELECT * FROM metrics WHERE recorded_at >= ? ORDER BY recorded_at",
            (since.isoformat(),),
        ).fetchall()
    return [dict(row) for row in rows]
