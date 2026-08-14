#!/usr/bin/env python3
from __future__ import annotations

import configparser
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from storage import Storage


DB_PATH = os.getenv("PAGER_DB_PATH", "/var/lib/racher-pager/pager.db")
STATE_ROOT = Path(os.getenv("PAGER_STATE_ROOT", str(Path(DB_PATH).parent)))
PDL_CONFIG_PATH = Path(os.getenv("PDL_CONFIG_PATH", str(STATE_ROOT / "pdl" / "pdl.ini")))
PDL_LOG_PATH = Path(os.getenv("PDL_LOG_PATH", str(STATE_ROOT / "pdl.log")))
PDL_BINARY = Path(os.getenv("PDL_BINARY", "/opt/racher-pager/pdl/bin/pdl"))
BACKUP_DIR = Path(os.getenv("PAGER_BACKUP_DIR", "/var/backups/racher-pager"))
POLL_SECONDS = max(1, int(os.getenv("PAGER_SYSTEM_AGENT_POLL_SECONDS", "2")))
STATUS_INTERVAL = max(5, int(os.getenv("PAGER_SYSTEM_STATUS_INTERVAL", "10")))

# Strict whitelist. No values from the database are interpolated into shell text.
COMMANDS: dict[str, list[str]] = {
    "restart-pdl": ["/usr/bin/systemctl", "restart", "racher-pdl.service"],
    "restart-gateway": ["/usr/bin/docker", "restart", "racher-pager-gateway"],
    "reboot": ["/usr/bin/systemctl", "reboot"],
}


def _iso_timestamp(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _run(argv: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return None


def _service_state(service_name: str) -> str:
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    result = _run([systemctl, "is-active", service_name], timeout=4)
    if result is None:
        return "unknown"
    state = (result.stdout or result.stderr or "").strip()
    return state or ("inactive" if result.returncode else "active")


def _gateway_container_state() -> str:
    docker = shutil.which("docker")
    if not docker:
        return "missing"
    result = _run(
        [docker, "inspect", "--format", "{{.State.Status}}", "racher-pager-gateway"],
        timeout=4,
    )
    if result is None or result.returncode != 0:
        return "missing"
    return (result.stdout or "unknown").strip() or "unknown"


def _audio_capture_status() -> tuple[int, str]:
    arecord = shutil.which("arecord")
    if not arecord:
        return 0, "alsa-utils/arecord mangler"
    result = _run([arecord, "-l"], timeout=5)
    if result is None:
        return 0, "kunne ikke køre arecord"
    text = (result.stdout or result.stderr or "").strip()
    cards = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^card\s+\d+:", line.strip(), flags=re.IGNORECASE)
    ]
    if not cards:
        return 0, "ingen ALSA capture-enheder fundet"
    summary = " | ".join(cards[:4])
    return len(cards), summary


def _cpu_temperature() -> str:
    candidates = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    for path in candidates:
        try:
            raw = float(path.read_text(encoding="utf-8").strip())
            value = raw / 1000.0 if raw > 200 else raw
            if -20 <= value <= 150:
                return f"{value:.1f}"
        except (OSError, ValueError):
            continue
    return ""


def _host_uptime_seconds() -> str:
    try:
        return str(int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])))
    except (OSError, ValueError, IndexError):
        return ""


def _backup_status() -> tuple[int, str]:
    try:
        backups = sorted(
            BACKUP_DIR.glob("racher-pager-*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0, ""
    if not backups:
        return 0, ""
    try:
        return len(backups), _iso_timestamp(backups[0].stat().st_mtime)
    except OSError:
        return len(backups), ""


def collect_runtime_status() -> dict[str, str]:
    audio_count, audio_summary = _audio_capture_status()
    backup_count, backup_latest = _backup_status()

    try:
        disk = shutil.disk_usage(STATE_ROOT)
        disk_total = str(disk.total)
        disk_free = str(disk.free)
    except OSError:
        disk_total = ""
        disk_free = ""

    try:
        pdl_stat = PDL_LOG_PATH.stat()
        pdl_log_exists = "1"
        pdl_log_size = str(pdl_stat.st_size)
        pdl_log_mtime = _iso_timestamp(pdl_stat.st_mtime)
    except OSError:
        pdl_log_exists = "0"
        pdl_log_size = "0"
        pdl_log_mtime = ""

    return {
        "agent_heartbeat": datetime.now(timezone.utc).isoformat(),
        "agent_pid": str(os.getpid()),
        "host_hostname": socket.gethostname(),
        "host_uptime_seconds": _host_uptime_seconds(),
        "cpu_temp_c": _cpu_temperature(),
        "disk_total_bytes": disk_total,
        "disk_free_bytes": disk_free,
        "pdl_installed": "1" if PDL_BINARY.exists() else "0",
        "pdl_service": _service_state("racher-pdl.service"),
        "gateway_container": _gateway_container_state(),
        "audio_capture_devices": str(audio_count),
        "audio_capture_summary": audio_summary,
        "pdl_log_exists": pdl_log_exists,
        "pdl_log_size": pdl_log_size,
        "pdl_log_mtime": pdl_log_mtime,
        "backup_count": str(backup_count),
        "backup_latest": backup_latest,
    }


def sync_pdl_settings(storage: Storage, config_path: Path = PDL_CONFIG_PATH) -> dict[str, str]:
    """Write web-managed decoder settings into PDL's existing pdl.ini.

    Only POCSAG baud enable flags and audio polarity are managed here. Hardware
    specific values such as CaptureDevice and SampleRate are deliberately
    preserved so configuring the scanner/USB sound card later cannot be undone
    by a web settings save.
    """
    settings = storage.get_settings()
    baud = str(settings.get("pocsag_baud", "auto")).strip().lower()
    if baud not in {"auto", "512", "1200", "2400"}:
        baud = "auto"

    invert_setting = str(settings.get("invert", "auto")).strip().lower()
    invert = "1" if invert_setting == "inverted" else "0"

    enabled = {
        "Baud512": "1" if baud in {"auto", "512"} else "0",
        "Baud1200": "1" if baud in {"auto", "1200"} else "0",
        "Baud2400": "1" if baud in {"auto", "2400"} else "0",
    }

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")

    if not parser.has_section("POCSAG"):
        parser.add_section("POCSAG")
    if not parser.has_section("Audio"):
        parser.add_section("Audio")

    parser.set("POCSAG", "Enable", "1")
    for key, value in enabled.items():
        parser.set("POCSAG", key, value)
    parser.set("Audio", "Invert", invert)

    if not parser.has_option("Audio", "CaptureDevice"):
        parser.set("Audio", "CaptureDevice", "default")
    if not parser.has_option("Audio", "SampleRate"):
        parser.set("Audio", "SampleRate", "48000")
    if not parser.has_option("Audio", "Config"):
        parser.set("Audio", "Config", "1")
    if not parser.has_option("Audio", "Enabled"):
        parser.set("Audio", "Enabled", "1")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o640)
    os.replace(tmp_path, config_path)

    return {
        "pocsag_baud": baud,
        "invert": "inverted" if invert == "1" else "normal",
    }


def run_command(storage: Storage, command: dict) -> None:
    command_id = int(command["id"])
    action = str(command["action"])
    argv = COMMANDS.get(action)
    if not argv:
        storage.finish_system_command(command_id, False, "Afvist: handling er ikke whitelistet")
        return

    if action == "reboot":
        storage.finish_system_command(command_id, True, "Reboot accepteret af host-agent")
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    try:
        prefix = ""
        if action == "restart-pdl":
            applied = sync_pdl_settings(storage)
            prefix = (
                f"PDL config: baud={applied['pocsag_baud']}, "
                f"polaritet={applied['invert']}. "
            )

        result = subprocess.run(argv, capture_output=True, text=True, timeout=45, check=False)
        text = (result.stdout or result.stderr or "OK").strip()
        storage.finish_system_command(
            command_id,
            result.returncode == 0,
            (prefix + text).strip(),
        )
    except Exception as exc:
        storage.finish_system_command(command_id, False, str(exc))


def main() -> int:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    print(f"Racher Pager system-agent bruger {DB_PATH}", flush=True)

    next_status = 0.0
    while True:
        now = time.monotonic()
        if now >= next_status:
            try:
                storage.update_runtime_status(collect_runtime_status())
            except Exception as exc:
                print(f"Kunne ikke gemme runtime-status: {exc}", flush=True)
            next_status = now + STATUS_INTERVAL

        command = storage.claim_next_system_command()
        if command:
            run_command(storage, command)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
