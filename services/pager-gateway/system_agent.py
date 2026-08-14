#!/usr/bin/env python3
from __future__ import annotations

import configparser
import os
import subprocess
import time
from pathlib import Path

from storage import Storage


DB_PATH = os.getenv("PAGER_DB_PATH", "/var/lib/racher-pager/pager.db")
PDL_CONFIG_PATH = Path(os.getenv("PDL_CONFIG_PATH", "/var/lib/racher-pager/pdl/pdl.ini"))
POLL_SECONDS = max(1, int(os.getenv("PAGER_SYSTEM_AGENT_POLL_SECONDS", "2")))

# Strict whitelist. No values from the database are interpolated into shell text.
COMMANDS: dict[str, list[str]] = {
    "restart-pdl": ["/usr/bin/systemctl", "restart", "racher-pdl.service"],
    "restart-gateway": ["/usr/bin/docker", "restart", "racher-pager-gateway"],
    "reboot": ["/usr/bin/systemctl", "reboot"],
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
    # GLib's GKeyFile reads the original mixed-case key names used by PDL.
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

    # Safe fallback values only when the config did not already have them.
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
        # Persist acknowledgement before systemd begins shutdown.
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
    while True:
        command = storage.claim_next_system_command()
        if command:
            run_command(storage, command)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
