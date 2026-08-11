#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


def run(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def read_temperature_c() -> float | None:
    vcgencmd = shutil.which("vcgencmd")
    if vcgencmd:
        result = run([vcgencmd, "measure_temp"], timeout=4)
        if result.returncode == 0 and "=" in result.stdout:
            try:
                return round(float(result.stdout.split("=", 1)[1].split("'", 1)[0]), 1)
            except ValueError:
                pass

    values: list[float] = []
    candidates = list(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    candidates += list(Path("/sys/class/hwmon").glob("hwmon*/temp*_input"))
    for path in candidates:
        try:
            raw = float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        value = raw / 1000 if raw > 1000 else raw
        if 1 <= value <= 110:
            values.append(value)
    return round(max(values), 1) if values else None


def memory_percent() -> int | None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if total <= 0:
        return None
    return round(((total - available) / total) * 100)


def uptime_seconds() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def docker_status() -> dict:
    result = run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
        timeout=12,
    )
    if result.returncode != 0:
        return {
            "available": False,
            "running": 0,
            "total": 0,
            "problems": ["docker utilgængelig"],
        }

    rows = [line for line in result.stdout.splitlines() if line.strip()]
    running = 0
    problems: list[str] = []
    for row in rows:
        name, _, status = row.partition("\t")
        if status.startswith("Up"):
            running += 1
            if "(unhealthy)" in status:
                problems.append(f"{name}: unhealthy")
        else:
            problems.append(f"{name}: {status or 'stoppet'}")
    return {
        "available": True,
        "running": running,
        "total": len(rows),
        "problems": problems,
    }


def failed_units() -> list[str]:
    result = run(["systemctl", "--failed", "--no-legend", "--plain"], timeout=8)
    if result.returncode not in {0, 1}:
        return ["systemd-status utilgængelig"]
    units = []
    for line in result.stdout.splitlines():
        unit = line.split(maxsplit=1)[0] if line.strip() else ""
        if unit:
            units.append(unit)
    return units


def collect() -> dict:
    disk_limit = int(os.getenv("RACHER_STATUS_DISK_PERCENT", "85"))
    memory_limit = int(os.getenv("RACHER_STATUS_MEMORY_PERCENT", "95"))
    temp_limit = float(os.getenv("RACHER_STATUS_TEMP_C", "80"))

    disk = shutil.disk_usage("/")
    disk_percent = round((disk.used / disk.total) * 100)
    mem_percent = memory_percent()
    temperature = read_temperature_c()
    docker = docker_status()
    failed = failed_units()
    issues: list[str] = []

    if disk_percent >= disk_limit:
        issues.append(f"disk {disk_percent}%")
    if mem_percent is not None and mem_percent >= memory_limit:
        issues.append(f"RAM {mem_percent}%")
    if temperature is not None and temperature >= temp_limit:
        issues.append(f"temperatur {temperature:.1f}C")
    if docker["problems"]:
        issues.extend(docker["problems"])
    if failed:
        issues.append("systemd: " + ", ".join(failed[:3]))

    try:
        load1 = round(os.getloadavg()[0], 2)
    except OSError:
        load1 = None

    return {
        "hostname": socket.gethostname(),
        "status": "error" if issues else "ok",
        "checked_at": int(time.time()),
        "temperature_c": temperature,
        "disk_percent": disk_percent,
        "memory_percent": mem_percent,
        "uptime_seconds": uptime_seconds(),
        "load1": load1,
        "docker": docker,
        "failed_units": failed,
        "issues": issues,
    }


def human_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    if days:
        return f"{days}d{hours}t"
    minutes = (remainder % 3600) // 60
    return f"{hours}t{minutes}m"


def compact(value: dict) -> str:
    label = value.get("hostname") or "server"
    state = "OK" if value.get("status") == "ok" else "FEJL"
    temp = value.get("temperature_c")
    temp_text = f"{temp:.0f}C" if isinstance(temp, (int, float)) else "?C"
    disk_text = f"D{value.get('disk_percent', '?')}%"
    ram_text = f"R{value.get('memory_percent', '?')}%"
    docker = value.get("docker") or {}
    docker_text = f"C{docker.get('running', '?')}/{docker.get('total', '?')}"
    uptime = human_uptime(value.get("uptime_seconds"))
    return f"{label} {state} {temp_text} {disk_text} {ram_text} {docker_text} U{uptime}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Saml lokal serverstatus")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    value = collect()
    if args.json:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    elif args.compact:
        print(compact(value))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
