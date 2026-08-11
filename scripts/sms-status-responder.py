#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/home/racher/Racher-Homelab")
ENV_FILE = Path(os.getenv("ENV_FILE", ROOT / ".env"))


def load_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def api_json(url: str, method: str = "GET", payload: dict | None = None) -> dict | None:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status == 204:
            return None
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def local_status() -> dict:
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/host-status.py"), "--json"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.stdout.strip():
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {
        "hostname": "racher-pi",
        "status": "error",
        "issues": [(result.stderr or "Kunne ikke læse lokal status").strip()[:160]],
    }


def remote_status() -> dict:
    path = ROOT / "scripts/remote-host-monitor.py"
    spec = importlib.util.spec_from_file_location("racher_remote_monitor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Kunne ikke indlæse remote-host-monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.remote_status()


def human_uptime(seconds) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    days, remainder = divmod(total, 86400)
    hours = remainder // 3600
    if days:
        return f"{days}d{hours}t"
    minutes = (remainder % 3600) // 60
    return f"{hours}t{minutes}m"


def status_sms(label: str, value: dict) -> str:
    state = "OK" if value.get("status") == "ok" else "FEJL"
    temp = value.get("temperature_c")
    temp_text = f"{temp:.0f}C" if isinstance(temp, (int, float)) else "?C"
    docker = value.get("docker") or {}
    base = (
        f"{label} {state} | temp {temp_text} | disk {value.get('disk_percent', '?')}% | "
        f"RAM {value.get('memory_percent', '?')}% | load {value.get('load1', '?')} | "
        f"Docker {docker.get('running', '?')}/{docker.get('total', '?')} | "
        f"up {human_uptime(value.get('uptime_seconds'))}"
    )
    issues = value.get("issues") or []
    if issues:
        room = 155 - len(base) - 3
        if room > 8:
            base += " | " + str(issues[0])[:room]
    return " ".join(base.split())[:155]


def queue_sms(recipient: str, body: str) -> None:
    api_base = os.getenv("SMS_GATEWAY_LOCAL_URL", "http://127.0.0.1:8090").rstrip("/")
    result = api_json(
        f"{api_base}/api/outgoing",
        method="POST",
        payload={"recipient": recipient, "body": body},
    )
    if not result or not result.get("id"):
        raise RuntimeError("SMS-gatewayen kvitterede ikke for svar-SMS")


def complete(command_id: int, status: str, error: str | None = None, retry: bool = False):
    api_base = os.getenv("SMS_GATEWAY_LOCAL_URL", "http://127.0.0.1:8090").rstrip("/")
    return api_json(
        f"{api_base}/api/commands/{command_id}/complete",
        method="POST",
        payload={"status": status, "error": error, "retry": retry},
    )


def process_command(command: dict) -> None:
    if command.get("command") != "status":
        raise RuntimeError(f"Ukendt SMS-kommando: {command.get('command')}")

    recipient = str(command.get("sender") or "").strip()
    if not recipient:
        raise RuntimeError("SMS-kommando mangler afsender")

    pi = local_status()
    mini = remote_status()
    queue_sms(recipient, status_sms("PI", pi))
    queue_sms(recipient, status_sms("MINI", mini))


def main() -> int:
    load_env(ENV_FILE)
    api_base = os.getenv("SMS_GATEWAY_LOCAL_URL", "http://127.0.0.1:8090").rstrip("/")
    poll_seconds = max(1.0, float(os.getenv("SMS_STATUS_POLL_SECONDS", "2")))

    print("SMS status responder startet.", flush=True)
    while True:
        try:
            command = api_json(
                f"{api_base}/api/commands/claim",
                method="POST",
                payload={},
            )
            if command is None:
                time.sleep(poll_seconds)
                continue

            try:
                process_command(command)
                complete(int(command["id"]), "done")
                print(
                    f"SMS-kommando {command['id']} ({command.get('command')}) besvaret.",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                complete(int(command["id"]), "failed", str(exc)[:1000], retry=True)
                print(f"SMS-kommando {command['id']} fejlede: {exc}", flush=True)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"Status responder kan ikke kontakte gatewayen: {exc}", flush=True)
            time.sleep(max(5.0, poll_seconds))
        except Exception as exc:  # noqa: BLE001
            print(f"Status responder fejlede: {exc}", flush=True)
            time.sleep(max(5.0, poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
