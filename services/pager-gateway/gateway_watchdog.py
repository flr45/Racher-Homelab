#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable


GATEWAY_PORT = int(os.getenv("PAGER_GATEWAY_PORT", "8088"))
HEALTH_URL = os.getenv("PAGER_GATEWAY_HEALTH_URL", f"http://127.0.0.1:{GATEWAY_PORT}/healthz")
CONTAINER_NAME = os.getenv("PAGER_GATEWAY_CONTAINER", "racher-pager-gateway")
FAILURE_THRESHOLD = max(1, int(os.getenv("PAGER_WATCHDOG_FAILURE_THRESHOLD", "3")))
RUNTIME_DIR = Path(os.getenv("PAGER_RUNTIME_DIR", "/run/racher-pager"))
STATE_FILE = Path(os.getenv("PAGER_WATCHDOG_STATE_FILE", str(RUNTIME_DIR / "gateway-watchdog.failures")))
MAINTENANCE_LOCK = Path(os.getenv("PAGER_MAINTENANCE_LOCK", str(RUNTIME_DIR / "maintenance.lock")))


def health_ok(url: str = HEALTH_URL, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200 and b'"ok":true' in response.read(512).replace(b" ", b"")
    except Exception:
        return False


def read_failures(path: Path = STATE_FILE) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def write_failures(value: int, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(max(0, int(value))), encoding="utf-8")
    os.replace(tmp, path)


def maintenance_in_progress(path: Path = MAINTENANCE_LOCK) -> tuple[bool, object | None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False, handle
    except BlockingIOError:
        handle.close()
        return True, None


def restart_gateway(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    try:
        result = run(
            ["/usr/bin/docker", "restart", CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main() -> int:
    maintenance, lock_handle = maintenance_in_progress()
    if maintenance:
        print("Pager watchdog: maintenance/update/restore kører; healthcheck springes over.", flush=True)
        return 0

    try:
        if health_ok():
            if read_failures():
                write_failures(0)
            print("Pager watchdog: gateway healthy.", flush=True)
            return 0

        failures = read_failures() + 1
        write_failures(failures)
        print(f"Pager watchdog: healthcheck fejlede ({failures}/{FAILURE_THRESHOLD}).", flush=True)
        if failures < FAILURE_THRESHOLD:
            return 0

        print("Pager watchdog: gateway svarer ikke; genstarter containeren.", flush=True)
        if not restart_gateway():
            print("Pager watchdog: container-genstart fejlede.", flush=True)
            return 1

        write_failures(0)
        for _ in range(10):
            time.sleep(2)
            if health_ok():
                print("Pager watchdog: gateway er healthy efter automatisk genstart.", flush=True)
                return 0
        print("Pager watchdog: gateway er stadig unhealthy efter automatisk genstart.", flush=True)
        return 1
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
