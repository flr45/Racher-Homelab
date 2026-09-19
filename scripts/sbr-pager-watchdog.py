#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
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

ROOT = Path(os.getenv("SBR_PAGER_ROOT", "/opt/SBR-Pager-Gateway"))
ENV_FILE = Path(os.getenv("ENV_FILE", ROOT / ".env"))
STATE_FILE = Path(
    os.getenv(
        "SBR_WATCHDOG_STATE_FILE",
        "/home/racher/.local/state/sbr-pager-watchdog/state.json",
    )
)
COMPONENTS = ("gateway", "pager", "openwa")
CONTAINERS = {
    "gateway": "racher-sms-gateway",
    "pager": "racher-sms-whatsapp",
    "openwa": "racher-sms-openwa",
}
_CREG_RE = re.compile(r"\+CREG:\s*\d+\s*,\s*(\d+)")


def load_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
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


def run(command: list[str], timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


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


def bind_url(prefix: str, default_port: int) -> str:
    bind = os.getenv(f"{prefix}_BIND_IP", "127.0.0.1").strip() or "127.0.0.1"
    if bind in {"0.0.0.0", "::"}:
        bind = "127.0.0.1"
    if ":" in bind and not bind.startswith("["):
        bind = f"[{bind}]"
    port = int(os.getenv(f"{prefix}_PORT", str(default_port)))
    return f"http://{bind}:{port}/health"


def http_json(url: str, timeout: int = 8) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("ugyldigt JSON-svar")
    return value


def docker_state(name: str) -> tuple[str, str]:
    result = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            name,
        ],
        timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return "missing", detail[:180] or "container mangler"
    status, _, health = result.stdout.strip().partition("|")
    return status or "unknown", health or "none"


def container_issue(component: str) -> str | None:
    status, health = docker_state(CONTAINERS[component])
    if status != "running":
        return f"{CONTAINERS[component]} er {status}"
    if health not in {"none", "healthy"}:
        return f"{CONTAINERS[component]} er {health}"
    return None


def registered_network(value: str) -> bool:
    match = _CREG_RE.search(value or "")
    return bool(match and int(match.group(1)) in {1, 5})


def collect_checks() -> tuple[dict[str, str | None], dict]:
    checks: dict[str, str | None] = {component: None for component in COMPONENTS}
    details: dict = {}

    for component in COMPONENTS:
        issue = container_issue(component)
        if issue:
            checks[component] = issue

    gateway_payload = None
    if checks["gateway"] is None:
        try:
            gateway_payload = http_json(bind_url("SMS_GATEWAY", 8090))
            details["gateway"] = gateway_payload
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            checks["gateway"] = f"SMS Gateway health svarer ikke: {exc}"

    if gateway_payload is not None and checks["gateway"] is None:
        if gateway_payload.get("status") != "ok":
            checks["gateway"] = f"SMS Gateway status={gateway_payload.get('status', 'ukendt')}"
        else:
            modem = gateway_payload.get("modem") or {}
            gateway = gateway_payload.get("gateway") or {}
            modem_state = str(modem.get("state", "unknown")).lower()
            database_state = str(gateway.get("database", "unknown")).lower()
            network = str(modem.get("network") or "")
            if modem_state != "online":
                checks["gateway"] = f"SMS-modem status={modem_state}"
            elif network and not registered_network(network):
                checks["gateway"] = "SMS-modem er ikke registreret på mobilnettet"
            elif database_state != "online":
                checks["gateway"] = f"SMS Gateway database={database_state}"

    pager_payload = None
    if checks["pager"] is None:
        try:
            pager_payload = http_json(bind_url("SMS_WHATSAPP", 8091))
            details["pager"] = pager_payload
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            checks["pager"] = f"SBR Pager health svarer ikke: {exc}"

    if pager_payload is not None and checks["pager"] is None:
        if pager_payload.get("status") != "ok":
            checks["pager"] = f"SBR Pager status={pager_payload.get('status', 'ukendt')}"
        elif str(pager_payload.get("database", "unknown")).lower() != "online":
            checks["pager"] = f"SBR Pager database={pager_payload.get('database', 'ukendt')}"
        else:
            queue = pager_payload.get("deliveryQueue") or {}
            failed = int(queue.get("failed", 0) or 0)
            oldest = queue.get("oldestMinutes")
            oldest_limit = float(os.getenv("SBR_WATCHDOG_RETRY_OLDEST_MINUTES", "15"))
            if failed > 0:
                checks["pager"] = f"WhatsApp retry-kø har {failed} permanent fejlede leveringer"
            elif isinstance(oldest, (int, float)) and oldest >= oldest_limit:
                checks["pager"] = f"WhatsApp retry-køens ældste besked er {oldest:.0f} min"

    if checks["openwa"] is None and pager_payload is not None:
        openwa = pager_payload.get("openwa") or {}
        state = str(openwa.get("state", "unknown")).lower()
        if state != "ready":
            checks["openwa"] = f"OpenWA session status={state}"

    return checks, details


def compose_up(component: str) -> str:
    if component == "gateway":
        compose_file = "compose/sms-gateway/docker-compose.yml"
        service = "sms-gateway"
    else:
        compose_file = "compose/sms-whatsapp/compose.yml"
        service = "sms-whatsapp" if component == "pager" else "openwa"

    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        compose_file,
        "up",
        "-d",
        "--no-deps",
        service,
    ]
    result = run(command, timeout=90)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-600:]
        raise RuntimeError(detail or f"compose up fejlede for {component}")
    return f"compose up {service}"


def recover_component(component: str) -> str:
    name = CONTAINERS[component]
    result = run(["docker", "restart", name], timeout=90)
    if result.returncode == 0:
        return f"docker restart {name}"
    return compose_up(component)


def smtp_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if env_bool("VAGTBYTTE_SMTP_ALLOW_SELF_SIGNED"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def send_email(subject: str, body: str) -> bool:
    recipient = (
        os.getenv("SBR_WATCHDOG_EMAIL_TO", "").strip()
        or os.getenv("RACHER_MONITOR_EMAIL_TO", "").strip()
    )
    host = os.getenv("VAGTBYTTE_SMTP_HOST", "").strip()
    username = os.getenv("VAGTBYTTE_SMTP_USER", "").strip()
    password = os.getenv("VAGTBYTTE_SMTP_PASSWORD", "")
    sender = os.getenv("VAGTBYTTE_SMTP_FROM", username).strip()
    port = int(os.getenv("VAGTBYTTE_SMTP_PORT", "587"))
    if not recipient or not host or not sender:
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("SBR Pager Watchdog", sender))
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
    return True


def queue_sms(body: str) -> bool:
    recipient = (
        os.getenv("SBR_WATCHDOG_SMS_TO", "").strip()
        or os.getenv("RACHER_MONITOR_SMS_TO", "").strip()
    )
    if not recipient:
        return False

    api_base = bind_url("SMS_GATEWAY", 8090).rsplit("/health", 1)[0]
    payload = json.dumps(
        {"recipient": recipient, "body": " ".join(body.split())[:155]},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{api_base}/api/outgoing",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("SMS Gateway kvitterede ikke")
    return True


def notify(subject: str, body: str, sms_body: str, *, allow_sms: bool) -> None:
    sent = []
    try:
        if send_email(subject, body):
            sent.append("mail")
    except Exception as exc:  # noqa: BLE001
        print(f"Watchdog-mail fejlede: {exc}")

    if allow_sms:
        try:
            if queue_sms(sms_body):
                sent.append("sms")
        except Exception as exc:  # noqa: BLE001
            print(f"Watchdog-SMS fejlede: {exc}")

    if sent:
        print("Watchdog-notifikation sendt via " + ", ".join(sent))
    else:
        print("Ingen watchdog-notifikationskanal er konfigureret eller tilgængelig")


def report_body(component: str, issue: str, action: str | None = None) -> str:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [
        "SBR Pager Watchdog",
        "",
        f"Tidspunkt: {timestamp}",
        f"Komponent: {component}",
        f"Fejl: {issue}",
    ]
    if action:
        lines.append(f"Automatisk handling: {action}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="SBR Pager watchdog")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    load_env(ENV_FILE)
    checks, details = collect_checks()

    if args.check_only:
        print(json.dumps({"checks": checks, "details": details}, indent=2, ensure_ascii=False))
        return 0 if not any(checks.values()) else 1

    now = int(time.time())
    threshold = max(1, int(os.getenv("SBR_WATCHDOG_FAIL_THRESHOLD", "3")))
    cooldown = max(60, int(os.getenv("SBR_WATCHDOG_RECOVERY_COOLDOWN_SECONDS", "600")))
    repeat_seconds = max(
        300,
        int(float(os.getenv("SBR_WATCHDOG_REPEAT_HOURS", "6")) * 3600),
    )

    state = load_state()
    components = state.setdefault("components", {})

    for component in COMPONENTS:
        issue = checks.get(component)
        item = components.setdefault(component, {})
        failures = int(item.get("failures", 0) or 0)
        alerted = bool(item.get("alerted", False))
        last_recovery = int(item.get("last_recovery_at", 0) or 0)
        last_notified = int(item.get("last_notified", 0) or 0)
        previous_issue = str(item.get("issue") or "")

        if not issue:
            if alerted:
                notify(
                    "[SBR Pager] Systemet fungerer igen",
                    report_body(component, "Fejlen er væk"),
                    f"SBR Pager OK: {component} fungerer igen.",
                    allow_sms=component != "gateway",
                )
            item.update(
                failures=0,
                alerted=False,
                issue=None,
                recovered_at=now if alerted else item.get("recovered_at"),
            )
            continue

        failures += 1
        item["failures"] = failures
        item["issue"] = issue
        item["last_seen_at"] = now
        print(f"{component}: FEJL {failures}/{threshold}: {issue}")

        action = None
        if failures >= threshold and now - last_recovery >= cooldown:
            try:
                action = recover_component(component)
                item["last_recovery_at"] = now
                item["last_recovery_action"] = action
                print(f"{component}: automatisk recovery udført: {action}")
            except Exception as exc:  # noqa: BLE001
                action = f"recovery fejlede: {exc}"
                item["last_recovery_at"] = now
                item["last_recovery_action"] = action
                print(f"{component}: {action}")

        should_notify = failures >= threshold and (
            not alerted
            or issue != previous_issue
            or now - last_notified >= repeat_seconds
        )
        if should_notify:
            notify(
                f"[SBR Pager] FEJL: {component}",
                report_body(component, issue, action),
                f"SBR Pager FEJL {component}: {issue}",
                allow_sms=component != "gateway" and checks.get("gateway") is None,
            )
            item["alerted"] = True
            item["last_notified"] = now

    state["checked_at"] = now
    state["checks"] = checks
    save_state(state)

    if any(checks.values()):
        print("Watchdog registrerede fejl; systemd-jobbet afsluttes normalt og prøver igen næste minut.")
    else:
        print("SBR Pager Watchdog: alle kontroller OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
