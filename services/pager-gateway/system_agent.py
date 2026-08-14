#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from storage import Storage


DB_PATH = os.getenv("PAGER_DB_PATH", "/var/lib/racher-pager/pager.db")
POLL_SECONDS = max(1, int(os.getenv("PAGER_SYSTEM_AGENT_POLL_SECONDS", "2")))

# Strict whitelist. No values from the database are interpolated into shell text.
COMMANDS: dict[str, list[str]] = {
    "restart-pdl": ["/usr/bin/systemctl", "restart", "racher-pdl.service"],
    "restart-gateway": ["/usr/bin/docker", "restart", "racher-pager-gateway"],
    "reboot": ["/usr/bin/systemctl", "reboot"],
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
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45, check=False)
        text = (result.stdout or result.stderr or "OK").strip()
        storage.finish_system_command(command_id, result.returncode == 0, text)
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
