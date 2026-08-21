from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


_CACHE_SECONDS = 8.0


def _age_seconds(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds())


def _endpoint_label(value: str) -> str:
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return "konfigureret"
    if not parsed.hostname:
        return "konfigureret"
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


class SystemOverview:
    """Small cached end-to-end health view for the Pager appliance."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self._lock = threading.RLock()
        self._sms_checked = 0.0
        self._sms_cache: dict[str, Any] = {"configured": False, "reachable": False}

    def _probe_sms_gateway(self) -> dict[str, Any]:
        base_url = str(os.getenv("PAGER_SMS_GATEWAY_URL", "") or "").strip().rstrip("/")
        if not base_url:
            return {
                "configured": False,
                "reachable": False,
                "endpoint": "",
                "status": "unset",
                "modem_state": "unknown",
            }

        now = time.monotonic()
        with self._lock:
            if now - self._sms_checked < _CACHE_SECONDS:
                return dict(self._sms_cache)

        result: dict[str, Any] = {
            "configured": True,
            "reachable": False,
            "endpoint": _endpoint_label(base_url),
            "status": "offline",
            "modem_state": "unknown",
            "modem_signal": "",
            "modem_network": "",
            "error": "",
        }
        request = urllib.request.Request(base_url + "/health", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=2.5) as response:
                raw = response.read().decode("utf-8")
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
            payload = json.loads(raw) if raw else {}
            modem = payload.get("modem") if isinstance(payload, dict) else {}
            modem = modem if isinstance(modem, dict) else {}
            result.update({
                "reachable": True,
                "status": str(payload.get("status") or "unknown"),
                "modem_state": str(modem.get("state") or "unknown"),
                "modem_signal": str(modem.get("signal") or ""),
                "modem_network": str(modem.get("network") or ""),
                "checked_at": str(payload.get("checked_at") or ""),
            })
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, json.JSONDecodeError, ValueError) as exc:
            result["error"] = str(exc)[:200]

        with self._lock:
            self._sms_checked = now
            self._sms_cache = dict(result)
        return result

    @staticmethod
    def _item(key: str, label: str, ok: bool, detail: str, *, warning: bool = False) -> dict[str, str]:
        return {
            "key": key,
            "label": label,
            "state": "ok" if ok else ("warning" if warning else "failed"),
            "detail": detail,
        }

    def snapshot(self, runtime: dict[str, Any]) -> dict[str, Any]:
        heartbeat_age = _age_seconds(runtime.get("agent_heartbeat"))
        agent_ok = heartbeat_age is not None and heartbeat_age <= 35
        fsk_connected = str(runtime.get("fsk_usb_connected") or "") == "1"
        fsk_in_use = str(runtime.get("fsk_usb_pdl_in_use") or "") == "1"
        pdl_ok = str(runtime.get("pdl_service") or "") == "active"
        gateway_ok = str(runtime.get("gateway_container") or "") == "running"
        internet_ok = str(runtime.get("internet_online") or "") == "1"

        sms = self._probe_sms_gateway()
        sms_ok = bool(sms.get("reachable")) and str(sms.get("status") or "").lower() == "ok"
        modem_ok = str(sms.get("modem_state") or "").lower() == "online"

        chain: list[dict[str, str]] = []
        boot_state = str(runtime.get("boot_verify_state") or "").lower()
        if boot_state:
            boot_ok = boot_state == "ok"
            boot_at = str(runtime.get("boot_verify_at") or "")
            attempts = str(runtime.get("boot_verify_attempts") or "")
            detail = {
                "ok": "Seneste opstart bestod hele end-to-end kontrollen",
                "waiting": "Boot-kontrollen venter stadig på et eller flere led",
                "degraded": "Lokal Pager startede, men ekstern SMS/GSM-kæde var ikke helt klar",
                "failed": "Et lokalt kritisk Pager-led kom ikke korrekt op efter boot",
            }.get(boot_state, f"Status {boot_state}")
            suffix = " · ".join(part for part in [f"forsøg {attempts}" if attempts else "", boot_at] if part)
            if suffix:
                detail += " · " + suffix
            chain.append(self._item(
                "boot-verify",
                "Seneste boot-verifikation",
                boot_ok,
                detail,
                warning=boot_state in {"waiting", "degraded"},
            ))

        chain.extend([
            self._item(
                "fsk",
                "FSK-USB / scanner",
                fsk_connected and fsk_in_use,
                "Forbundet og åbnet af PDL" if fsk_connected and fsk_in_use
                else "FSK-USB fundet, men PDL bruger den ikke" if fsk_connected
                else "FSK-USB ikke registreret",
                warning=fsk_connected,
            ),
            self._item(
                "pdl",
                "PDL decoder",
                pdl_ok,
                "racher-pdl.service kører" if pdl_ok else f"Service: {runtime.get('pdl_service') or 'ukendt'}",
            ),
            self._item(
                "gateway",
                "Pager Gateway",
                gateway_ok,
                "Container kører og denne status-API svarer" if gateway_ok else f"Container: {runtime.get('gateway_container') or 'ukendt'}",
            ),
            self._item(
                "agent",
                "System-agent",
                agent_ok,
                f"Heartbeat {int(heartbeat_age)} sek. siden" if heartbeat_age is not None else "Intet heartbeat",
            ),
            self._item(
                "network",
                "Internet",
                internet_ok,
                "Online" if internet_ok else "Ingen bekræftet internetforbindelse",
                warning=not internet_ok,
            ),
            self._item(
                "sms-link",
                "Tailscale / SMS-link",
                bool(sms.get("reachable")),
                f"{sms.get('endpoint')} svarer" if sms.get("reachable")
                else f"Kan ikke nå {sms.get('endpoint') or 'SMS Gateway'}",
                warning=bool(sms.get("configured")) and not sms.get("reachable"),
            ),
            self._item(
                "sms-gateway",
                "SMS Gateway",
                sms_ok,
                f"Status {sms.get('status')}" if sms.get("reachable") else "Afventer forbindelse",
                warning=bool(sms.get("reachable")) and not sms_ok,
            ),
            self._item(
                "gsm",
                "GSM modem",
                modem_ok,
                " · ".join(
                    part for part in [
                        str(sms.get("modem_state") or "unknown"),
                        str(sms.get("modem_signal") or ""),
                        str(sms.get("modem_network") or ""),
                    ] if part
                ),
                warning=bool(sms.get("reachable")) and not modem_ok,
            ),
        ])

        local_ready = fsk_connected and fsk_in_use and pdl_ok and gateway_ok and agent_ok
        end_to_end_ready = local_ready and bool(sms.get("reachable")) and sms_ok and modem_ok
        return {
            "state": "ok" if end_to_end_ready else ("local-ok" if local_ready else "degraded"),
            "local_ready": local_ready,
            "end_to_end_ready": end_to_end_ready,
            "chain": chain,
            "sms": sms,
            "boot_verify_state": boot_state,
            "host_uptime_seconds": runtime.get("host_uptime_seconds") or "",
        }


def install_system_overview(core: Any) -> SystemOverview:
    overview = SystemOverview(core)
    original_status = core.app.view_functions["api_status"]

    def status_with_overview(*args, **kwargs):
        response = original_status(*args, **kwargs)
        if isinstance(response, tuple) or getattr(response, "status_code", 200) >= 400:
            return response
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict):
            return response
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        payload["system_overview"] = overview.snapshot(runtime)
        return core.jsonify(payload)

    core.app.view_functions["api_status"] = status_with_overview
    return overview
