#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/home/racher/Racher-Homelab")
ENV_FILE = Path(os.getenv("ENV_FILE", ROOT / ".env"))
STATE_FILE = Path(
    os.getenv(
        "RACHER_REMOTE_MONITOR_STATE_FILE",
        "/home/racher/homelab/data/remote-host-monitor/state.json",
    )
)


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


def load_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(value: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_FILE)


def api_json(url: str, method: str = "GET", payload: dict | None = None) -> dict | None:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def queue_sms(body: str) -> None:
    recipient = os.getenv("RACHER_MONITOR_SMS_TO", "").strip()
    if not recipient:
        raise RuntimeError("RACHER_MONITOR_SMS_TO mangler")
    api_base = os.getenv("SMS_GATEWAY_LOCAL_URL", "http://127.0.0.1:8090").rstrip("/")
    result = api_json(
        f"{api_base}/api/outgoing",
        method="POST",
        payload={"recipient": recipient, "body": " ".join(body.split())[:155]},
    )
    if not result or not result.get("id"):
        raise RuntimeError("SMS-gatewayen kvitterede ikke for alarmen")


def remote_status() -> dict:
    target = os.getenv("RACHER_REMOTE_HOST", "racher@racherserver.local").strip()
    key = os.getenv(
        "RACHER_REMOTE_SSH_KEY",
        "/home/racher/.ssh/racherserver_monitor_ed25519",
    ).strip()
    remote_script = os.getenv(
        "RACHER_REMOTE_STATUS_SCRIPT",
        "/home/racher/Racher-Homelab/scripts/host-status.py",
    ).strip()

    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key:
        command.extend(["-i", key])
    command.extend([target, "python3", remote_script, "--json"])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "hostname": "racherserver",
            "status": "offline",
            "issues": [f"SSH utilgængelig: {exc}"],
        }

    if result.stdout.strip():
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass

    detail = (result.stderr or result.stdout).strip().splitlines()
    reason = detail[-1] if detail else f"SSH exit {result.returncode}"
    return {
        "hostname": "racherserver",
        "status": "offline",
        "issues": [reason[:180]],
    }


def compact_metrics(value: dict) -> str:
    temp = value.get("temperature_c")
    temp_text = f"{temp:.0f}C" if isinstance(temp, (int, float)) else "?C"
    docker = value.get("docker") or {}
    return (
        f"{temp_text}, disk {value.get('disk_percent', '?')}%, "
        f"RAM {value.get('memory_percent', '?')}%, "
        f"Docker {docker.get('running', '?')}/{docker.get('total', '?')}"
    )


def main() -> int:
    load_env(ENV_FILE)
    status = remote_status()
    issues = status.get("issues") or []
    healthy = status.get("status") == "ok"
    fingerprint = hashlib.sha256(
        json.dumps(
            {"status": status.get("status"), "issues": issues},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    now = int(time.time())
    state = load_state()
    failures = int(state.get("failures", 0) or 0)
    alerted = bool(state.get("alerted", False))
    previous_fingerprint = state.get("fingerprint")
    down_since = int(state.get("down_since", 0) or 0)
    last_notified = int(state.get("last_notified", 0) or 0)
    threshold = max(1, int(os.getenv("RACHER_REMOTE_FAIL_THRESHOLD", "3")))
    repeat_seconds = int(float(os.getenv("RACHER_REMOTE_REPEAT_HOURS", "6")) * 3600)

    if healthy:
        if alerted:
            downtime_minutes = max(1, round((now - (down_since or now)) / 60))
            queue_sms(
                f"Racherserver ONLINE igen. Nedetid ca. {downtime_minutes} min. "
                + compact_metrics(status)
            )
            print("Recovery-SMS sat i kø.")
        save_state(
            {
                "failures": 0,
                "alerted": False,
                "fingerprint": fingerprint,
                "down_since": None,
                "last_notified": now if alerted else last_notified,
                "last_status": status,
                "checked_at": now,
            }
        )
        print("Racherserver: ONLINE/OK")
        return 0

    failures += 1
    if not down_since:
        down_since = now

    should_notify = failures >= threshold and (
        not alerted
        or previous_fingerprint != fingerprint
        or now - last_notified >= repeat_seconds
    )

    if should_notify:
        reason = issues[0] if issues else "ukendt fejl"
        queue_sms(f"Racherserver FEJL: {reason}. {compact_metrics(status)}")
        alerted = True
        last_notified = now
        print("Alarm-SMS sat i kø.")

    save_state(
        {
            "failures": failures,
            "alerted": alerted,
            "fingerprint": fingerprint,
            "down_since": down_since,
            "last_notified": last_notified,
            "last_status": status,
            "checked_at": now,
        }
    )
    print(f"Racherserver: FEJL {failures}/{threshold} - {(issues or ['ukendt fejl'])[0]}")
    # En fjernfejl er et monitorresultat, ikke en fejl i selve systemd-jobbet.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
