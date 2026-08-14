#!/usr/bin/env python3
from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import Storage, validate_system_command


DB_PATH = os.getenv("PAGER_DB_PATH", "/var/lib/racher-pager/pager.db")
STATE_ROOT = Path(os.getenv("PAGER_STATE_ROOT", str(Path(DB_PATH).parent)))
PDL_CONFIG_PATH = Path(os.getenv("PDL_CONFIG_PATH", str(STATE_ROOT / "pdl" / "pdl.ini")))
PDL_LOG_PATH = Path(os.getenv("PDL_LOG_PATH", str(STATE_ROOT / "pdl.log")))
PDL_BINARY = Path(os.getenv("PDL_BINARY", "/opt/racher-pager/pdl/bin/pdl"))
BACKUP_DIR = Path(os.getenv("PAGER_BACKUP_DIR", "/var/backups/racher-pager"))
INTEGRATION_DIR = Path(os.getenv("PAGER_INTEGRATION_DIR", "/opt/racher-pager/integration"))
RUNTIME_REPO = Path(os.getenv("PAGER_RUNTIME_REPO", "/opt/racher-pager/runtime-repo"))
DEPLOY_BRANCH = os.getenv("PAGER_DEPLOY_BRANCH", "main")
PUBLIC_HOSTNAME = os.getenv("PAGER_PUBLIC_HOSTNAME", "")
WIFI_IFACE = os.getenv("PAGER_WIFI_IFACE", "wlan0")
HOTSPOT_CONNECTION = os.getenv("PAGER_HOTSPOT_CONNECTION", "Racher-Pager-Setup")
HOTSPOT_SSID = os.getenv("PAGER_HOTSPOT_SSID", "Racher-Pager-Setup")
HOTSPOT_PASSWORD = os.getenv("PAGER_HOTSPOT_PASSWORD", "")
HOTSPOT_FALLBACK_SECONDS = max(60, int(os.getenv("PAGER_HOTSPOT_FALLBACK_SECONDS", "180")))
HOTSPOT_CYCLE_SECONDS = max(300, int(os.getenv("PAGER_HOTSPOT_CYCLE_SECONDS", "900")))
POLL_SECONDS = max(1, int(os.getenv("PAGER_SYSTEM_AGENT_POLL_SECONDS", "2")))
STATUS_INTERVAL = max(5, int(os.getenv("PAGER_SYSTEM_STATUS_INTERVAL", "10")))
AUTO_HOTSPOT_MARKER = Path("/run/racher-pager-hotspot-auto")

BACKUP_SCRIPT = INTEGRATION_DIR / "backup-pager.sh"
RESTORE_SCRIPT = INTEGRATION_DIR / "restore-pager.sh"
UPDATE_SCRIPT = INTEGRATION_DIR / "update-pager.sh"
ROLLBACK_SCRIPT = INTEGRATION_DIR / "rollback-pager.sh"

# Strict fixed commands. Dynamic actions below still use argv arrays and
# separately validated payloads; no database value is executed as shell text.
COMMANDS: dict[str, list[str]] = {
    "restart-pdl": ["/usr/bin/systemctl", "restart", "racher-pdl.service"],
    "restart-gateway": ["/usr/bin/docker", "restart", "racher-pager-gateway"],
    "reboot": ["/usr/bin/systemctl", "reboot"],
    "restart-tunnel": ["/usr/bin/systemctl", "restart", "cloudflared.service"],
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


def _command_text(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "kommando kunne ikke køres"
    return (result.stdout or result.stderr or "OK").strip()


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
    return len(cards), " | ".join(cards[:4])


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


def _backup_catalog() -> list[dict[str, Any]]:
    try:
        paths = sorted(
            BACKUP_DIR.glob("racher-pager-*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []

    result: list[dict[str, Any]] = []
    for path in paths[:30]:
        try:
            stat = path.stat()
        except OSError:
            continue
        result.append({
            "filename": path.name,
            "created_at": _iso_timestamp(stat.st_mtime),
            "size": stat.st_size,
        })
    return result


def _internet_online() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            return True
    except OSError:
        return False


def _nmcli() -> str | None:
    return shutil.which("nmcli")


def _wifi_current_connection(nmcli: str, iface: str) -> str:
    result = _run([nmcli, "-g", "GENERAL.CONNECTION", "device", "show", iface], timeout=4)
    if result is None or result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    return "" if value == "--" else value


def _wifi_signal(nmcli: str, iface: str) -> str:
    result = _run([nmcli, "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list", "ifname", iface], timeout=6)
    if result is None or result.returncode != 0:
        return ""
    for line in (result.stdout or "").splitlines():
        if line.startswith("*:"):
            return line.split(":", 1)[1].strip()
    return ""


def _wifi_ip(nmcli: str, iface: str) -> str:
    result = _run([nmcli, "-g", "IP4.ADDRESS", "device", "show", iface], timeout=4)
    if result is None or result.returncode != 0:
        return ""
    first = next((line.strip() for line in (result.stdout or "").splitlines() if line.strip()), "")
    return first.split("/", 1)[0]


def _managed_wifi_profiles(nmcli: str) -> list[dict[str, str]]:
    result = _run([nmcli, "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5)
    if result is None or result.returncode != 0:
        return []
    profiles: list[dict[str, str]] = []
    for line in (result.stdout or "").splitlines():
        name, _, kind = line.rpartition(":")
        name = name.replace("\\:", ":").replace("\\\\", "\\")
        if kind == "802-11-wireless" and name.startswith("racher-wifi-"):
            profiles.append({"profile": name})
    return profiles


def _network_status() -> dict[str, str]:
    nmcli = _nmcli()
    internet = _internet_online()
    if not nmcli:
        return {
            "network_manager": "missing",
            "wifi_iface": WIFI_IFACE,
            "wifi_connection": "",
            "wifi_signal_percent": "",
            "wifi_ip": "",
            "wifi_profiles_json": "[]",
            "hotspot_active": "0",
            "internet_online": "1" if internet else "0",
        }

    current = _wifi_current_connection(nmcli, WIFI_IFACE)
    profiles = _managed_wifi_profiles(nmcli)
    return {
        "network_manager": "ready",
        "wifi_iface": WIFI_IFACE,
        "wifi_connection": current,
        "wifi_signal_percent": _wifi_signal(nmcli, WIFI_IFACE),
        "wifi_ip": _wifi_ip(nmcli, WIFI_IFACE),
        "wifi_profiles_json": json.dumps(profiles, ensure_ascii=False),
        "hotspot_active": "1" if current == HOTSPOT_CONNECTION else "0",
        "hotspot_ssid": HOTSPOT_SSID,
        "hotspot_password": HOTSPOT_PASSWORD,
        "hotspot_portal": "http://10.42.0.1/",
        "internet_online": "1" if internet else "0",
    }


def _tunnel_status() -> dict[str, str]:
    binary = shutil.which("cloudflared")
    version = ""
    if binary:
        result = _run([binary, "--version"], timeout=4)
        if result and result.returncode == 0:
            version = (result.stdout or result.stderr or "").strip()
    return {
        "tunnel_installed": "1" if binary else "0",
        "tunnel_service": _service_state("cloudflared.service") if binary else "missing",
        "tunnel_version": version,
        "public_hostname": PUBLIC_HOSTNAME,
    }


def _update_status() -> dict[str, str]:
    current = ""
    previous = ""
    current_file = STATE_ROOT / "update" / "current-sha"
    previous_file = STATE_ROOT / "update" / "previous-sha"
    try:
        current = current_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        previous = previous_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not current and (RUNTIME_REPO / ".git").exists():
        result = _run(["/usr/bin/git", "-C", str(RUNTIME_REPO), "rev-parse", "HEAD"], timeout=4)
        if result and result.returncode == 0:
            current = (result.stdout or "").strip()
    return {
        "deploy_branch": DEPLOY_BRANCH,
        "deploy_current_sha": current,
        "deploy_previous_sha": previous,
    }


def collect_runtime_status() -> dict[str, str]:
    audio_count, audio_summary = _audio_capture_status()
    backups = _backup_catalog()

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

    values = {
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
        "backup_count": str(len(backups)),
        "backup_latest": backups[0]["created_at"] if backups else "",
        "backup_catalog_json": json.dumps(backups, ensure_ascii=False),
    }
    values.update(_network_status())
    values.update(_tunnel_status())
    values.update(_update_status())
    return values


def sync_pdl_settings(storage: Storage, config_path: Path = PDL_CONFIG_PATH) -> dict[str, str]:
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
    return {"pocsag_baud": baud, "invert": "inverted" if invert == "1" else "normal"}


def _wifi_profile_name(ssid: str) -> str:
    return "racher-wifi-" + hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:10]


def _wifi_add(payload: dict[str, Any]) -> tuple[bool, str]:
    nmcli = _nmcli()
    if not nmcli:
        return False, "NetworkManager/nmcli mangler"
    ssid = str(payload["ssid"])
    password = str(payload["password"])
    profile = _wifi_profile_name(ssid)

    existing = _run([nmcli, "-g", "NAME", "connection", "show", profile], timeout=4)
    if existing and existing.returncode == 0:
        _run([nmcli, "connection", "delete", profile], timeout=10)

    create = _run([
        nmcli, "connection", "add", "type", "wifi", "ifname", WIFI_IFACE,
        "con-name", profile, "ssid", ssid,
    ], timeout=15)
    if create is None or create.returncode != 0:
        return False, _command_text(create)

    modify = _run([
        nmcli, "connection", "modify", profile,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "100",
        "ipv4.method", "auto",
        "ipv6.method", "auto",
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password,
    ], timeout=15)
    if modify is None or modify.returncode != 0:
        _run([nmcli, "connection", "delete", profile], timeout=10)
        return False, _command_text(modify)

    AUTO_HOTSPOT_MARKER.unlink(missing_ok=True)
    if _wifi_current_connection(nmcli, WIFI_IFACE) == HOTSPOT_CONNECTION:
        _run([nmcli, "connection", "down", HOTSPOT_CONNECTION], timeout=10)
    connect = _run([nmcli, "connection", "up", profile], timeout=35)
    if connect and connect.returncode == 0:
        return True, f"Wi-Fi-profil gemt og aktiveret: {ssid}"
    return True, f"Wi-Fi-profil gemt: {ssid}. Forbindelsen prøves automatisk igen."


def _wifi_remove(payload: dict[str, Any]) -> tuple[bool, str]:
    nmcli = _nmcli()
    if not nmcli:
        return False, "NetworkManager/nmcli mangler"
    profile = str(payload["profile"])
    result = _run([nmcli, "connection", "delete", profile], timeout=15)
    return bool(result and result.returncode == 0), _command_text(result)


def _hotspot(start: bool, automatic: bool = False) -> tuple[bool, str]:
    nmcli = _nmcli()
    if not nmcli:
        return False, "NetworkManager/nmcli mangler"
    argv = [nmcli, "connection", "up" if start else "down", HOTSPOT_CONNECTION]
    result = _run(argv, timeout=20)
    ok = bool(result and result.returncode == 0)
    if start and ok and automatic:
        AUTO_HOTSPOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        AUTO_HOTSPOT_MARKER.write_text(str(int(time.time())), encoding="utf-8")
    elif not automatic:
        AUTO_HOTSPOT_MARKER.unlink(missing_ok=True)
    return ok, _command_text(result)


def _run_script(script: Path, args: list[str] | None = None, timeout: int = 900) -> tuple[bool, str]:
    if not script.exists():
        return False, f"Mangler helper: {script}"
    result = _run([str(script), *(args or [])], timeout=timeout)
    return bool(result and result.returncode == 0), _command_text(result)


def _schedule_restore(storage: Storage, command_id: int, payload: dict[str, Any]) -> None:
    filename = str(payload["filename"])
    if not RESTORE_SCRIPT.exists():
        storage.finish_system_command(command_id, False, f"Mangler helper: {RESTORE_SCRIPT}")
        return
    storage.finish_system_command(command_id, True, f"Restore planlagt: {filename}. Gateway genstarter.")
    subprocess.Popen(
        [str(RESTORE_SCRIPT), filename],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def run_command(storage: Storage, command: dict[str, Any]) -> None:
    command_id = int(command["id"])
    action = str(command["action"])
    try:
        payload = validate_system_command(action, command.get("payload"))
    except ValueError as exc:
        storage.finish_system_command(command_id, False, f"Afvist af host-agent: {exc}")
        return

    if action == "reboot":
        storage.finish_system_command(command_id, True, "Reboot accepteret af host-agent")
        subprocess.Popen(COMMANDS[action], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    if action == "restore-backup":
        _schedule_restore(storage, command_id, payload)
        return

    try:
        prefix = ""
        if action == "restart-pdl":
            applied = sync_pdl_settings(storage)
            prefix = f"PDL config: baud={applied['pocsag_baud']}, polaritet={applied['invert']}. "
            result = _run(COMMANDS[action], timeout=45)
            ok = bool(result and result.returncode == 0)
            text = _command_text(result)
        elif action in {"restart-gateway", "restart-tunnel"}:
            result = _run(COMMANDS[action], timeout=45)
            ok = bool(result and result.returncode == 0)
            text = _command_text(result)
        elif action == "backup-now":
            ok, text = _run_script(BACKUP_SCRIPT, timeout=180)
        elif action == "update-gateway":
            ok, text = _run_script(UPDATE_SCRIPT, timeout=1200)
        elif action == "rollback-gateway":
            ok, text = _run_script(ROLLBACK_SCRIPT, timeout=1200)
        elif action == "wifi-add":
            ok, text = _wifi_add(payload)
        elif action == "wifi-remove":
            ok, text = _wifi_remove(payload)
        elif action == "hotspot-start":
            ok, text = _hotspot(True, automatic=False)
        elif action == "hotspot-stop":
            ok, text = _hotspot(False, automatic=False)
        else:
            storage.finish_system_command(command_id, False, "Afvist: handling er ikke implementeret")
            return
        storage.finish_system_command(command_id, ok, (prefix + text).strip())
    except Exception as exc:
        storage.finish_system_command(command_id, False, str(exc))


def maybe_manage_hotspot(status: dict[str, str], state: dict[str, float | None]) -> None:
    if status.get("network_manager") != "ready":
        state["offline_since"] = None
        return

    online = status.get("internet_online") == "1"
    active = status.get("hotspot_active") == "1"
    now = time.monotonic()

    if online:
        state["offline_since"] = None
        if active and AUTO_HOTSPOT_MARKER.exists():
            _hotspot(False, automatic=True)
            AUTO_HOTSPOT_MARKER.unlink(missing_ok=True)
        return

    if active:
        state["offline_since"] = None
        if AUTO_HOTSPOT_MARKER.exists():
            try:
                started = float(AUTO_HOTSPOT_MARKER.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                started = time.time()
            if time.time() - started >= HOTSPOT_CYCLE_SECONDS:
                # Give saved infrastructure profiles another chance periodically.
                _hotspot(False, automatic=True)
                AUTO_HOTSPOT_MARKER.unlink(missing_ok=True)
                state["offline_since"] = now
        return

    if state.get("offline_since") is None:
        state["offline_since"] = now
        return
    if now - float(state["offline_since"] or now) >= HOTSPOT_FALLBACK_SECONDS:
        _hotspot(True, automatic=True)
        state["offline_since"] = None


def main() -> int:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    print(f"Racher Pager system-agent bruger {DB_PATH}", flush=True)

    next_status = 0.0
    network_state: dict[str, float | None] = {"offline_since": None}
    while True:
        now = time.monotonic()
        if now >= next_status:
            try:
                runtime = collect_runtime_status()
                storage.update_runtime_status(runtime)
                maybe_manage_hotspot(runtime, network_state)
            except Exception as exc:
                print(f"Kunne ikke gemme/runtime-styre status: {exc}", flush=True)
            next_status = now + STATUS_INTERVAL

        command = storage.claim_next_system_command()
        if command:
            run_command(storage, command)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
