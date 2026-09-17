from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import build_config
from secure_store import load_json, save_json
from storage import add_event, connection, get_setting, set_setting, utcnow_iso

CREDENTIAL_NAME = "system-link-credentials"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _as_float(key: str, default: float, minimum: float, maximum: float) -> float:
    raw = get_setting(key, str(default))
    try:
        value = float(raw or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def ensure_system_link_schema() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS system_link_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbound_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_attempt_at TEXT,
                delivered_at TEXT,
                FOREIGN KEY(inbound_id) REFERENCES inbound_messages(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_system_link_status
                ON system_link_deliveries(status, last_attempt_at);
            """
        )


def _credentials() -> dict | None:
    try:
        payload = load_json(CREDENTIAL_NAME)
    except Exception as exc:  # noqa: BLE001
        add_event("error", "system_link_credentials", f"Kunne ikke læse DPAPI credentials: {exc}")
        return None
    if not payload:
        return None
    required = ("client_id", "client_secret", "message_endpoint")
    if not all(str(payload.get(key) or "").strip() for key in required):
        return None
    return payload


def bootstrap_configured() -> bool:
    return bool(
        str(build_config.SYSTEM_LINK_PROVISION_URL or "").strip()
        and str(build_config.SYSTEM_LINK_PROVISION_TOKEN or "").strip()
    )


def system_link_enabled() -> bool:
    return _as_bool(get_setting("system_link_enabled", "0"))


def system_link_status() -> dict:
    ensure_system_link_schema()
    credentials = _credentials()
    with connection() as db:
        counts = db.execute(
            """
            SELECT
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
              MAX(delivered_at) AS last_success
            FROM system_link_deliveries
            """
        ).fetchone()
    return {
        "enabled": system_link_enabled(),
        "provisioned": credentials is not None,
        "bootstrap_configured": bootstrap_configured(),
        "client_id": str(credentials.get("client_id") or "") if credentials else "",
        "endpoint": (
            str(credentials.get("message_endpoint") or "")
            if credentials
            else (get_setting("system_link_endpoint", "") or "")
        ),
        "pending": int(counts["pending"] or 0),
        "failed": int(counts["failed"] or 0),
        "sent": int(counts["sent"] or 0),
        "last_success": counts["last_success"],
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_request(url: str, payload: dict, timeout: float) -> dict:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "SBR-Pager-Gateway/Provisioning",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise RuntimeError("System Link returnerede et ugyldigt svar")
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001
            detail = None
        raise RuntimeError(detail or f"System Link svarede HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"System Link kunne ikke forbindes: {exc.reason}") from exc


def ensure_provisioned(timeout: float = 8.0) -> dict | None:
    existing = _credentials()
    if existing:
        return existing
    if not bootstrap_configured():
        return None

    result = _json_request(
        str(build_config.SYSTEM_LINK_PROVISION_URL).strip(),
        {
            "token": str(build_config.SYSTEM_LINK_PROVISION_TOKEN),
            "machineName": socket.gethostname(),
            "appVersion": str(build_config.APP_VERSION or "unknown"),
        },
        timeout=max(2.0, min(float(timeout), 30.0)),
    )
    client_id = str(result.get("clientId") or "").strip()
    client_secret = str(result.get("clientSecret") or "").strip()
    endpoint = str(
        result.get("messageEndpoint")
        or build_config.SYSTEM_LINK_MESSAGE_ENDPOINT
        or ""
    ).strip()
    if not client_id or not client_secret or not endpoint.startswith("https://"):
        raise RuntimeError("Provisionering returnerede ikke gyldige System Link credentials")

    credentials = {
        "client_id": client_id,
        "client_secret": client_secret,
        "message_endpoint": endpoint,
        "provisioned_at": utcnow_iso(),
    }
    save_json(CREDENTIAL_NAME, credentials)
    if not get_setting("system_link_started_at", None):
        set_setting("system_link_started_at", utcnow_iso())
    set_setting("system_link_endpoint", endpoint)
    set_setting("system_link_client_id", client_id)
    set_setting("system_link_enabled", "1")
    add_event("info", "system_link_provisioned", f"System Link registreret som {client_id}")
    return credentials


def _active_identity() -> tuple[str, str, str | None] | None:
    credentials = _credentials()
    if credentials:
        return (
            str(credentials["message_endpoint"]),
            str(credentials["client_secret"]),
            str(credentials["client_id"]),
        )

    # Migration path for development/older builds that used a manually entered
    # shared key. New release installers do not expose this in the UI.
    endpoint = (get_setting("system_link_endpoint", "") or "").strip()
    secret = get_setting("system_link_token", "") or ""
    if endpoint and secret:
        return endpoint, secret, None
    return None


def _payload_for_message(inbound_id: int, client_id: str | None) -> dict | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT id, source_id, sender, body, received_at, modem_port,
                   accepted, processing_status, created_at
            FROM inbound_messages
            WHERE id=? AND accepted=1
            """,
            (inbound_id,),
        ).fetchone()
        if not row:
            return None

        event = db.execute(
            """
            SELECT e.id, e.station, e.alarm_type, e.address, e.status,
                   e.followup_count, e.started_at, e.completed_at
            FROM alarm_event_messages em
            JOIN alarm_events e ON e.id=em.event_id
            WHERE em.inbound_id=?
            LIMIT 1
            """,
            (inbound_id,),
        ).fetchone()

    source_id = str(row["source_id"])
    namespace = client_id or "legacy"
    payload = {
        "schema": "sbr-pager-gateway.system-link.v1",
        "gateway": "SBR Pager Gateway",
        "deliveryId": f"{namespace}:{source_id}",
        "sentAt": utcnow_iso(),
        "message": {
            "id": int(row["id"]),
            "sourceId": source_id,
            "sender": str(row["sender"]),
            "body": str(row["body"]),
            "receivedAt": str(row["received_at"]),
            "createdAt": str(row["created_at"]),
            "modemPort": row["modem_port"],
            "processingStatus": str(row["processing_status"]),
        },
        "event": None,
    }
    if event:
        payload["event"] = {
            "id": int(event["id"]),
            "station": event["station"],
            "alarmType": event["alarm_type"],
            "address": event["address"],
            "status": event["status"],
            "followupCount": int(event["followup_count"] or 0),
            "startedAt": event["started_at"],
            "completedAt": event["completed_at"],
        }
    return payload


def _request(
    payload: dict,
    *,
    endpoint: str,
    secret: str,
    client_id: str | None,
    timeout: float,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "SBR-Pager-Gateway/System-Link",
        "X-System-Link-Signature": f"sha256={signature}",
    }
    if client_id:
        headers["X-System-Link-Client"] = client_id
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise RuntimeError(f"System Link svarede HTTP {status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"System Link svarede HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"System Link kunne ikke forbindes: {exc.reason}") from exc


def test_system_link(timeout: float = 5.0) -> None:
    credentials = ensure_provisioned(timeout=timeout)
    identity = _active_identity()
    if not credentials and not identity:
        raise RuntimeError("System Link er endnu ikke provisioneret")
    assert identity is not None
    endpoint, secret, client_id = identity
    payload = {
        "schema": "sbr-pager-gateway.system-link.v1",
        "gateway": "SBR Pager Gateway",
        "deliveryId": f"test:{client_id or 'legacy'}:{utcnow_iso()}",
        "test": True,
        "sentAt": utcnow_iso(),
    }
    _request(
        payload,
        endpoint=endpoint,
        secret=secret,
        client_id=client_id,
        timeout=max(1.0, min(timeout, 30.0)),
    )


class SystemLinkEngine:
    """Independent background mirror for accepted alarm data.

    Provisioning and delivery run independently from WhatsApp. A System Link
    outage must therefore never delay or block the alarm's WhatsApp delivery.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._next_provision_attempt = 0.0
        ensure_system_link_schema()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if _credentials() is None and bootstrap_configured():
                    now = time.monotonic()
                    if now >= self._next_provision_attempt:
                        self._next_provision_attempt = now + 30.0
                        ensure_provisioned()
                if system_link_enabled() and _active_identity():
                    self._queue_accepted_messages()
                    self._process_one()
            except Exception as exc:  # noqa: BLE001
                add_event("error", "system_link_worker", str(exc)[:1000])
            self._stop.wait(1.0)

    def _queue_accepted_messages(self) -> None:
        started_at = get_setting("system_link_started_at", None)
        if not started_at:
            set_setting("system_link_started_at", utcnow_iso())
            return
        with connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO system_link_deliveries(inbound_id, status, attempts)
                SELECT id, 'pending', 0
                FROM inbound_messages
                WHERE accepted=1 AND created_at>=?
                """,
                (started_at,),
            )

    def _process_one(self) -> None:
        identity = _active_identity()
        if identity is None:
            return
        endpoint, secret, client_id = identity
        if not endpoint.lower().startswith("https://") and client_id:
            return

        retry_seconds = _as_float("system_link_retry_seconds", 30.0, 5.0, 3600.0)
        timeout = _as_float("system_link_timeout_seconds", 5.0, 1.0, 30.0)
        now = datetime.now(timezone.utc)

        with connection() as db:
            rows = db.execute(
                """
                SELECT id, inbound_id, status, attempts, last_attempt_at
                FROM system_link_deliveries
                WHERE status IN ('pending', 'failed')
                ORDER BY id ASC
                LIMIT 25
                """
            ).fetchall()

        candidate = None
        for row in rows:
            last = _parse_iso(row["last_attempt_at"])
            if last is None or (now - last).total_seconds() >= retry_seconds:
                candidate = dict(row)
                break
        if candidate is None:
            return

        delivery_id = int(candidate["id"])
        inbound_id = int(candidate["inbound_id"])
        payload = _payload_for_message(inbound_id, client_id)
        if payload is None:
            return

        attempt_time = utcnow_iso()
        try:
            _request(
                payload,
                endpoint=endpoint,
                secret=secret,
                client_id=client_id,
                timeout=timeout,
            )
            with connection() as db:
                db.execute(
                    """
                    UPDATE system_link_deliveries
                    SET status='sent', attempts=attempts+1, last_error=NULL,
                        last_attempt_at=?, delivered_at=?
                    WHERE id=?
                    """,
                    (attempt_time, utcnow_iso(), delivery_id),
                )
            add_event("info", "system_link_sent", f"SMS {inbound_id} videresendt via System Link")
        except Exception as exc:  # noqa: BLE001
            with connection() as db:
                db.execute(
                    """
                    UPDATE system_link_deliveries
                    SET status='failed', attempts=attempts+1, last_error=?, last_attempt_at=?
                    WHERE id=?
                    """,
                    (str(exc)[:1000], attempt_time, delivery_id),
                )
            add_event("error", "system_link_failed", f"SMS {inbound_id}: {str(exc)[:500]}")
            time.sleep(0.05)
