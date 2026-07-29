import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

MAX_TEXT_LENGTH = 512
THROTTLE_FLAGS = {
    0: "under_voltage_now",
    1: "frequency_capped_now",
    2: "throttled_now",
    3: "soft_temperature_limit_now",
    16: "under_voltage_occurred",
    17: "frequency_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temperature_limit_occurred",
}


def _clean(value, limit=MAX_TEXT_LENGTH):
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit] or None


def _read_text(path, *, reader=None):
    try:
        if reader:
            return _clean(reader(Path(path)))
        return _clean(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _read_model(reader=None):
    return _read_text("/proc/device-tree/model", reader=reader) or _read_text(
        "/sys/firmware/devicetree/base/model", reader=reader
    )


def _read_temperature(reader=None):
    raw = _read_text("/sys/class/thermal/thermal_zone0/temp", reader=reader)
    try:
        return round(float(raw) / 1000, 1) if raw is not None else None
    except ValueError:
        return None


def _throttle_status(runner=subprocess.run):
    try:
        completed = runner(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "raw": None, "flags": []}
    output = _clean(completed.stdout, 100)
    if completed.returncode != 0 or not output or "=" not in output:
        return {"available": False, "raw": output, "flags": []}
    try:
        value = int(output.split("=", 1)[1], 16)
    except ValueError:
        return {"available": False, "raw": output, "flags": []}
    flags = [name for bit, name in THROTTLE_FLAGS.items() if value & (1 << bit)]
    return {"available": True, "raw": f"0x{value:x}", "flags": flags}


def _storage(path="/"):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round((usage.used / usage.total) * 100, 1)
        if usage.total
        else 0.0,
    }


def _interfaces():
    try:
        return sorted(name for _, name in socket.if_nameindex())[:64]
    except OSError:
        return []


def _docker_host(docker_factory=None):
    try:
        if docker_factory is None:
            import docker

            docker_factory = docker.from_env
        client = docker_factory()
        client.ping()
        info = client.info()
        version = client.version()
        return {
            "available": True,
            "server_version": _clean(version.get("Version"), 100),
            "operating_system": _clean(info.get("OperatingSystem"), 200),
            "architecture": _clean(info.get("Architecture"), 100),
            "cpus": info.get("NCPU"),
            "memory_bytes": info.get("MemTotal"),
            "containers": info.get("Containers"),
        }
    except Exception:
        return {"available": False}


def node_inventory(*, reader=None, runner=subprocess.run, docker_factory=None):
    uname = platform.uname()
    temperature = _read_temperature(reader=reader)
    throttling = _throttle_status(runner=runner)
    storage = _storage(os.getenv("RACHER_OS_DATA", "/data")) or _storage("/")
    warnings = []
    if temperature is not None and temperature >= 75:
        warnings.append("high_temperature")
    if throttling["flags"]:
        warnings.append("power_or_throttling_event")
    if storage and storage["used_percent"] >= 85:
        warnings.append("storage_pressure")
    return {
        "hostname": _clean(socket.gethostname(), 255),
        "model": _read_model(reader=reader),
        "architecture": _clean(platform.machine(), 100),
        "system": _clean(uname.system, 100),
        "kernel": _clean(uname.release, 200),
        "python": _clean(platform.python_version(), 100),
        "cpu_count": os.cpu_count(),
        "temperature_c": temperature,
        "throttling": throttling,
        "storage": storage,
        "network_interfaces": _interfaces(),
        "docker": _docker_host(docker_factory=docker_factory),
        "warnings": warnings,
        "healthy": not warnings,
    }
