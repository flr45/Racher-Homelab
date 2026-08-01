#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import smtplib
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

ROOT = Path("/home/racher/Racher-Homelab")
ENV_FILE = Path(os.getenv("ENV_FILE", ROOT / ".env"))
STATE_FILE = Path(
    os.getenv(
        "RACHER_MONITOR_STATE_FILE",
        "/home/racher/homelab/data/health-monitor/state.json",
    )
)
DEFAULT_CONTAINERS = (
    "nginx-proxy-manager,control-center,portainer,uptime-kuma,npm-db,"
    "postgres,redis,vagtbytte-web,vagtbytte-worker,racher-sms-gateway,"
    "minutregnskab,cloudflared"
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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def age_hours(timestamp: float) -> float:
    return max(0.0, (time.time() - timestamp) / 3600)


def check_containers(issues: list[str]) -> None:
    configured = os.getenv("RACHER_MONITOR_CONTAINERS", DEFAULT_CONTAINERS)
    for name in [item.strip() for item in configured.split(",") if item.strip()]:
        result = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                name,
            ]
        )
        if result.returncode != 0:
            issues.append(f"Container mangler: {name}")
            continue

        status, _, health = result.stdout.strip().partition("|")
        if status != "running":
            issues.append(f"Container {name} har status {status or 'ukendt'}")
        elif health not in {"none", "healthy"}:
            issues.append(f"Container {name} er {health}")


def check_failed_units(issues: list[str]) -> None:
    result = run(["systemctl", "--failed", "--no-legend", "--plain"])
    if result.returncode not in {0, 1}:
        issues.append("Kunne ikke læse fejlede systemd-tjenester")
        return

    failed = []
    for line in result.stdout.splitlines():
        unit = line.split(maxsplit=1)[0] if line.strip() else ""
        if unit and unit != "racher-health-monitor.service":
            failed.append(unit)
    if failed:
        issues.append("Fejlede systemd-tjenester: " + ", ".join(failed))


def check_disk(issues: list[str]) -> None:
    threshold = int(os.getenv("RACHER_MONITOR_DISK_PERCENT", "85"))
    usage = shutil.disk_usage("/")
    percent = round((usage.used / usage.total) * 100)
    if percent >= threshold:
        issues.append(f"Diskforbrug er {percent}% (grænse {threshold}%)")


def check_homelab_backup(issues: list[str]) -> None:
    max_hours = float(os.getenv("RACHER_MONITOR_BACKUP_MAX_HOURS", "30"))
    backup_root = Path(os.getenv("BACKUP_ROOT", "/home/racher/homelab/backups"))
    latest = backup_root / "latest"
    try:
        target = latest.resolve(strict=True)
        backup_age = age_hours(target.stat().st_mtime)
    except OSError:
        issues.append("Homelab-backuppen mangler")
        return

    if backup_age > max_hours:
        issues.append(f"Homelab-backuppen er {backup_age:.1f} timer gammel")


def check_vagtbytte_backup(issues: list[str]) -> None:
    max_hours = float(os.getenv("RACHER_MONITOR_BACKUP_MAX_HOURS", "30"))
    result = run(
        [
            "docker",
            "exec",
            "vagtbytte-web",
            "sh",
            "-lc",
            "latest=$(ls -1t /data/backups/*.vagtbackup.enc 2>/dev/null | head -1); "
            "test -n \"$latest\" || exit 2; stat -c %Y \"$latest\"",
        ]
    )
    if result.returncode != 0:
        issues.append("Krypteret Vagtbytte-backup mangler")
        return

    try:
        backup_age = age_hours(float(result.stdout.strip()))
    except ValueError:
        issues.append("Kunne ikke aflæse tidspunktet på Vagtbytte-backuppen")
        return

    if backup_age > max_hours:
        issues.append(f"Vagtbytte-backuppen er {backup_age:.1f} timer gammel")


def check_power_and_temperature(issues: list[str]) -> None:
    throttled = run(["vcgencmd", "get_throttled"])
    if throttled.returncode == 0 and "0x" in throttled.stdout:
        try:
            value = int(throttled.stdout.strip().split("0x", 1)[1], 16)
            active = value & 0xF
            if active:
                issues.append(f"Aktiv strøm-/throttling-advarsel: 0x{active:x}")
        except ValueError:
            issues.append("Kunne ikke aflæse Raspberry Pi-strømstatus")

    temperature = run(["vcgencmd", "measure_temp"])
    if temperature.returncode == 0 and "=" in temperature.stdout:
        try:
            value = float(temperature.stdout.split("=", 1)[1].split("'", 1)[0])
            limit = float(os.getenv("RACHER_MONITOR_TEMP_C", "80"))
            if value >= limit:
                issues.append(f"Pi-temperaturen er {value:.1f} °C")
        except ValueError:
            issues.append("Kunne ikke aflæse Raspberry Pi-temperaturen")


def check_sms_gateway(issues: list[str]) -> None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        issues.append(f"SMS-gatewayen svarer ikke: {exc}")
        return

    if payload.get("status") != "ok":
        issues.append(f"SMS-gatewayens status er {payload.get('status', 'ukendt')}")
    modem_state = str(payload.get("modem", {}).get("state", "unknown")).lower()
    if modem_state != "online":
        issues.append(f"SMS-modemets status er {modem_state}")
    database_state = str(payload.get("gateway", {}).get("database", "unknown")).lower()
    if database_state != "online":
        issues.append(f"SMS-gatewayens database er {database_state}")


def collect_issues() -> list[str]:
    issues: list[str] = []
    check_containers(issues)
    check_failed_units(issues)
    check_disk(issues)
    check_homelab_backup(issues)
    check_vagtbytte_backup(issues)
    check_power_and_temperature(issues)
    check_sms_gateway(issues)
    return sorted(set(issues))


def smtp_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if env_bool("VAGTBYTTE_SMTP_ALLOW_SELF_SIGNED"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def send_email(subject: str, body: str) -> None:
    host = os.getenv("VAGTBYTTE_SMTP_HOST", "").strip()
    username = os.getenv("VAGTBYTTE_SMTP_USER", "").strip()
    password = os.getenv("VAGTBYTTE_SMTP_PASSWORD", "")
    sender = os.getenv("VAGTBYTTE_SMTP_FROM", username).strip()
    recipient = os.getenv("RACHER_MONITOR_EMAIL_TO", "info@racher.dk").strip()
    port = int(os.getenv("VAGTBYTTE_SMTP_PORT", "587"))

    if not host or not sender or not recipient:
        raise RuntimeError("SMTP eller monitorens mailmodtager mangler")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Racher OS", sender))
    message["To"] = recipient
    message.set_content(body)

    context = smtp_context()
    if env_bool("VAGTBYTTE_SMTP_SECURE"):
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=20)

    with client:
        client.ehlo()
        if not env_bool("VAGTBYTTE_SMTP_SECURE") and env_bool(
            "VAGTBYTTE_SMTP_STARTTLS", True
        ):
            client.starttls(context=context)
            client.ehlo()
        if username:
            client.login(username, password)
        client.send_message(message)


def sms_text(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())[:155]


def send_sms(body: str) -> None:
    recipient = os.getenv("RACHER_MONITOR_SMS_TO", "").strip()
    if not recipient:
        raise RuntimeError("RACHER_MONITOR_SMS_TO mangler")

    code = (
        "import os; from app import send_sms; "
        "send_sms(os.environ['ALERT_TO'], os.environ['ALERT_BODY'])"
    )
    result = run(
        [
            "docker",
            "exec",
            "-e",
            f"ALERT_TO={recipient}",
            "-e",
            f"ALERT_BODY={sms_text(body)}",
            "racher-sms-gateway",
            "python",
            "-c",
            code,
        ],
        timeout=45,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()[-500:]
        raise RuntimeError(details or "SMS-afsendelsen fejlede")


def notify(subject: str, body: str, sms_body: str) -> tuple[bool, list[str]]:
    delivered = False
    errors: list[str] = []
    try:
        send_email(subject, body)
        delivered = True
        print("Mail sendt.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Mail: {exc}")
        print(f"Mail fejlede: {exc}")

    try:
        send_sms(sms_body)
        delivered = True
        print("SMS sendt.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"SMS: {exc}")
        print(f"SMS fejlede: {exc}")

    return delivered, errors


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


def format_report(issues: list[str], heading: str) -> str:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [heading, "", f"Tidspunkt: {timestamp}", f"Vært: {os.uname().nodename}"]
    if issues:
        lines.extend(["", "Fund:"])
        lines.extend(f"- {issue}" for issue in issues)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Racher OS health monitor")
    parser.add_argument("--test-notifications", action="store_true")
    args = parser.parse_args()

    load_env(ENV_FILE)

    if args.test_notifications:
        subject = "[Racher OS] Test af driftsalarm"
        body = format_report([], "Dette er en test af mail- og SMS-alarmen.")
        delivered, errors = notify(
            subject,
            body,
            "Racher OS TEST: Mail- og SMS-alarmen virker.",
        )
        if errors:
            print(" | ".join(errors))
        return 0 if delivered and not errors else 1

    issues = collect_issues()
    status = "error" if issues else "ok"
    fingerprint = hashlib.sha256("\n".join(issues).encode("utf-8")).hexdigest()
    now = int(time.time())
    state = load_state()
    previous_status = state.get("status")
    previous_fingerprint = state.get("fingerprint")
    last_notified = int(state.get("last_notified", 0) or 0)
    repeat_seconds = int(float(os.getenv("RACHER_MONITOR_REPEAT_HOURS", "6")) * 3600)

    should_notify = False
    if status == "error":
        should_notify = (
            previous_status != "error"
            or previous_fingerprint != fingerprint
            or now - last_notified >= repeat_seconds
        )
    elif previous_status == "error":
        should_notify = True

    if should_notify and status == "error":
        subject = f"[Racher OS] FEJL ({len(issues)})"
        body = format_report(issues, "Racher OS har registreret en driftsfejl.")
        sms = f"Racher OS FEJL: {issues[0]}. Mail sendt med detaljer."
        delivered, errors = notify(subject, body, sms)
        if errors:
            print(" | ".join(errors))
        if delivered:
            last_notified = now
    elif should_notify:
        subject = "[Racher OS] Systemet fungerer igen"
        body = format_report([], "De tidligere registrerede fejl er væk.")
        delivered, errors = notify(
            subject,
            body,
            "Racher OS OK: De tidligere fejl er væk.",
        )
        if errors:
            print(" | ".join(errors))
        if delivered:
            last_notified = now

    save_state(
        {
            "status": status,
            "fingerprint": fingerprint,
            "issues": issues,
            "checked_at": now,
            "last_notified": last_notified,
        }
    )

    if issues:
        print("Driftsfejl:")
        for issue in issues:
            print(f"- {issue}")
    else:
        print("Alle kontroller er OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
