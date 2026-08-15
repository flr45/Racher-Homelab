#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TARGET = os.getenv("PAGER_MONITOR_TARGET", "racher-pi2").strip()
BASE_URL = os.getenv("PAGER_MONITOR_URL", f"http://{TARGET}:8088").rstrip("/")
SMS_GATEWAY_URL = os.getenv("PAGER_MONITOR_SMS_GATEWAY", "http://127.0.0.1:8090").rstrip("/")
STATE_FILE = Path(os.getenv("PAGER_MONITOR_STATE_FILE", "/var/lib/racher-pager-monitor/state.json"))
HTTP_TIMEOUT = max(1.0, float(os.getenv("PAGER_MONITOR_HTTP_TIMEOUT", "5")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_state(path: Path | None = None) -> dict[str, Any]:
    target = path or STATE_FILE
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def get_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Ugyldigt JSON-svar")
    return payload


def health_ok() -> bool:
    try:
        payload = get_json(f"{BASE_URL}/healthz")
        return payload.get("ok") is True and payload.get("database") == "ok"
    except Exception:
        return False


def fetch_config() -> dict[str, Any]:
    payload = get_json(f"{BASE_URL}/api/external-monitor/config")
    if payload.get("ok") is not True:
        raise RuntimeError("Monitor-konfiguration blev afvist")
    return payload


def tailscale_reachable(target: str = TARGET) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/tailscale", "ping", "--c=1", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def send_sms(recipient: str, body: str) -> bool:
    payload = json.dumps({"recipient": recipient, "body": body}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{SMS_GATEWAY_URL}/api/outgoing",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status in {200, 201, 202}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def outage_minutes(started_at: str | None) -> int:
    if not started_at:
        return 0
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        seconds = max(0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
        return max(1, round(seconds / 60))
    except ValueError:
        return 0


def main() -> int:
    state = read_state()
    state.setdefault("failure_count", 0)
    state.setdefault("alarm_active", False)
    state.setdefault("enabled", False)
    state.setdefault("failure_threshold", 3)
    state.setdefault("sms_to", "")
    state["last_check_at"] = now_iso()

    healthy = health_ok()
    if healthy:
        try:
            config = fetch_config()
            state["enabled"] = bool(config.get("enabled"))
            state["sms_to"] = str(config.get("sms_to") or "").strip()
            state["failure_threshold"] = max(1, min(int(config.get("failure_threshold") or 3), 10))
            state["gateway_name"] = str(config.get("gateway_name") or "Racher Pager Gateway")[:80]
            state["config_cached_at"] = now_iso()
            state["last_config_error"] = ""
        except Exception as exc:
            state["last_config_error"] = str(exc)[:300]

        state["failure_count"] = 0
        state["last_ok_at"] = now_iso()
        state["last_error"] = ""
        if state.get("alarm_active"):
            minutes = outage_minutes(state.get("outage_started_at"))
            recipient = str(state.get("sms_to") or "").strip()
            name = str(state.get("gateway_name") or "Racher Pager Gateway")
            body = f"{name} OK: Pager-systemet er online igen. Nedetid ca. {minutes} min."
            if recipient and send_sms(recipient, body):
                state["alarm_active"] = False
                state["recovery_sent_at"] = now_iso()
                state["outage_started_at"] = ""
        write_state(state)
        print("Pager external monitor: healthy", flush=True)
        return 0

    reachable = tailscale_reachable()
    reason = "gateway svarer ikke" if reachable else "Pi/netvaerk kan ikke naas via Tailscale"
    state["last_error"] = reason
    state["failure_count"] = int(state.get("failure_count") or 0) + 1
    if not state.get("outage_started_at"):
        state["outage_started_at"] = now_iso()

    if not state.get("enabled"):
        write_state(state)
        print(f"Pager external monitor: {reason}; SMS-overvaagning er ikke aktiveret", flush=True)
        return 0

    threshold = max(1, min(int(state.get("failure_threshold") or 3), 10))
    recipient = str(state.get("sms_to") or "").strip()
    if state["failure_count"] >= threshold and not state.get("alarm_active"):
        if not recipient:
            state["last_sms_error"] = "Intet cached fejl-SMS nummer"
        else:
            name = str(state.get("gateway_name") or "Racher Pager Gateway")
            body = f"{name} FEJL: {reason}. Fejl registreret {state['failure_count']} gange i traek."
            if send_sms(recipient, body):
                state["alarm_active"] = True
                state["alarm_sent_at"] = now_iso()
                state["last_sms_error"] = ""
            else:
                state["last_sms_error"] = "SMS-gateway kunne ikke koe beskeden"

    write_state(state)
    print(f"Pager external monitor: {reason} ({state['failure_count']}/{threshold})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
