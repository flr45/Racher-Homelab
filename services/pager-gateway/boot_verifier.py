#!/usr/bin/env python3
from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DB_PATH = os.getenv("PAGER_DB_PATH", "/var/lib/racher-pager/pager.db")
GATEWAY_PORT = int(os.getenv("PAGER_GATEWAY_PORT", "8088"))
SMS_GATEWAY_URL = str(os.getenv("PAGER_SMS_GATEWAY_URL", "") or "").strip().rstrip("/")
ATTEMPTS = max(1, min(int(os.getenv("PAGER_BOOT_VERIFY_ATTEMPTS", "18")), 60))
INTERVAL = max(1, min(int(os.getenv("PAGER_BOOT_VERIFY_INTERVAL_SECONDS", "5")), 30))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(argv: list[str], timeout: int = 4) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout or result.stderr or "").strip()


def service_active(name: str) -> bool:
    code, output = run(["systemctl", "is-active", name])
    return code == 0 and output == "active"


def http_json(url: str, timeout: float = 2.5) -> dict[str, Any] | None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
        return payload if isinstance(payload, dict) else None
    except (
        TimeoutError,
        ConnectionError,
        http.client.HTTPException,
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ):
        # During boot, Docker/Gunicorn/Tailscale can expose a socket before the
        # service behind it is ready to complete HTTP. Python can surface that as
        # a bare timeout, connection reset/close (RemoteDisconnected), malformed
        # early HTTP response or the usual urllib errors. All are transient probe
        # failures here: return not-ready and let the outer boot loop retry.
        return None


def runtime_values() -> dict[str, str]:
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as conn:
            rows = conn.execute("SELECT key, value FROM runtime_status").fetchall()
        return {str(key): str(value) for key, value in rows}
    except sqlite3.Error:
        return {}


def tailscale_status() -> dict[str, Any]:
    binary = shutil.which("tailscale")
    if not binary:
        return {"installed": False, "service": "missing", "ip": ""}
    active = service_active("tailscaled.service")
    code, output = run([binary, "ip", "-4"], timeout=5)
    ip = output.splitlines()[0].strip() if code == 0 and output.strip() else ""
    return {"installed": True, "service": "active" if active else "inactive", "ip": ip}


def check_once() -> dict[str, Any]:
    runtime = runtime_values()
    gateway = http_json(f"http://127.0.0.1:{GATEWAY_PORT}/healthz", timeout=2.0)
    sms = http_json(SMS_GATEWAY_URL + "/health", timeout=2.5) if SMS_GATEWAY_URL else None
    modem = sms.get("modem", {}) if isinstance(sms, dict) else {}
    if not isinstance(modem, dict):
        modem = {}

    checks = {
        "gateway": bool(gateway and gateway.get("ok")),
        "pdl": service_active("racher-pdl.service"),
        "system_agent": service_active("racher-pager-system-agent.service"),
        "fsk_connected": runtime.get("fsk_usb_connected") == "1",
        "fsk_in_use": runtime.get("fsk_usb_pdl_in_use") == "1",
        "sms_gateway": bool(sms and str(sms.get("status") or "").lower() == "ok"),
        "gsm_modem": str(modem.get("state") or "").lower() == "online",
    }
    tailscale = tailscale_status()
    checks["tailscale"] = bool(
        not tailscale["installed"] or (tailscale["service"] == "active" and tailscale["ip"])
    )

    local_ready = all(
        checks[key]
        for key in ("gateway", "pdl", "system_agent", "fsk_connected", "fsk_in_use")
    )
    remote_required = bool(SMS_GATEWAY_URL)
    remote_ready = (not remote_required) or (
        checks["tailscale"] and checks["sms_gateway"] and checks["gsm_modem"]
    )
    return {
        "local_ready": local_ready,
        "remote_ready": remote_ready,
        "end_to_end_ready": local_ready and remote_ready,
        "checks": checks,
        "tailscale": tailscale,
        "sms_configured": remote_required,
        "sms_status": str(sms.get("status") or "offline") if isinstance(sms, dict) else "offline",
        "gsm_state": str(modem.get("state") or "unknown"),
    }


def write_status(result: dict[str, Any], attempt: int, state: str) -> None:
    values = {
        "boot_verify_state": state,
        "boot_verify_at": now_iso(),
        "boot_verify_attempts": str(attempt),
        "boot_verify_local_ready": "1" if result.get("local_ready") else "0",
        "boot_verify_remote_ready": "1" if result.get("remote_ready") else "0",
        "boot_verify_end_to_end_ready": "1" if result.get("end_to_end_ready") else "0",
        "boot_verify_detail_json": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    }
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        boot_id = ""
    values["boot_id"] = boot_id

    for db_attempt in range(5):
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS runtime_status (
                           key TEXT PRIMARY KEY,
                           value TEXT NOT NULL,
                           updated_at TEXT NOT NULL
                       )"""
                )
                timestamp = now_iso()
                conn.executemany(
                    """INSERT INTO runtime_status(key,value,updated_at) VALUES (?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    [(key, value, timestamp) for key, value in values.items()],
                )
            return
        except sqlite3.OperationalError:
            if db_attempt == 4:
                raise
            time.sleep(1)


def main() -> int:
    last: dict[str, Any] = {
        "local_ready": False,
        "remote_ready": False,
        "end_to_end_ready": False,
        "checks": {},
    }
    for attempt in range(1, ATTEMPTS + 1):
        last = check_once()
        state = "ok" if last["end_to_end_ready"] else "waiting"
        write_status(last, attempt, state)
        if last["end_to_end_ready"]:
            print(json.dumps({"state": "ok", "attempt": attempt, **last}, ensure_ascii=False))
            return 0
        if attempt < ATTEMPTS:
            time.sleep(INTERVAL)

    final_state = "degraded" if last.get("local_ready") else "failed"
    write_status(last, ATTEMPTS, final_state)
    print(json.dumps({"state": final_state, "attempt": ATTEMPTS, **last}, ensure_ascii=False))
    # Local failures are a genuine boot failure. A remote SMS/GSM outage is
    # recorded as degraded but must not put the Pi into a systemd restart loop.
    return 0 if last.get("local_ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
