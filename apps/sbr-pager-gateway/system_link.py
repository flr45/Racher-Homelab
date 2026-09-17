from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from storage import add_event, connection, get_setting, utcnow_iso


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


def system_link_enabled() -> bool:
    return _as_bool(get_setting("system_link_enabled", "0"))


def system_link_status() -> dict:
    ensure_system_link_schema()
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
        "endpoint": get_setting("system_link_endpoint", "") or "",
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


def _payload_for_message(inbound_id: int) -> dict | None:
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

    payload = {
        "schema": "sbr-pager-gateway.system-link.v1",
        "gateway": "SBR Pager Gateway",
        "message": {
            "id": int(row["id"]),
            "sourceId": str(row["source_id"]),
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


def _request(payload: dict, *, endpoint: str, token: str, timeout: float) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "SBR-Pager-Gateway/System-Link",
    }
    if token:
        signature = hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-System-Link-Signature"] = f"sha256={signature}"
        headers["X-System-Link-Token"] = token
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


def test_system_link(endpoint: str, token: str, timeout: float = 5.0) -> None:
    endpoint = endpoint.strip()
    if not endpoint.lower().startswith(("http://", "https://")):
        raise ValueError("Endpoint skal starte med http:// eller https://")
    payload = {
        "schema": "sbr-pager-gateway.system-link.v1",
        "gateway": "SBR Pager Gateway",
        "test": True,
        "sentAt": utcnow_iso(),
    }
    _request(payload, endpoint=endpoint, token=token, timeout=max(1.0, min(timeout, 30.0)))


class SystemLinkEngine:
    """Independent background mirror for accepted alarm data.

    Failure here must never delay or block WhatsApp delivery. Each accepted
    inbound SMS is queued once and retried until the configured endpoint
    acknowledges it with a 2xx response.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        ensure_system_link_schema()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if system_link_enabled():
                    self._queue_accepted_messages()
                    self._process_one()
            except Exception as exc:  # noqa: BLE001
                add_event("error", "system_link_worker", str(exc)[:1000])
            self._stop.wait(1.0)

    def _queue_accepted_messages(self) -> None:
        with connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO system_link_deliveries(inbound_id, status, attempts)
                SELECT id, 'pending', 0
                FROM inbound_messages
                WHERE accepted=1
                """
            )

    def _process_one(self) -> None:
        endpoint = (get_setting("system_link_endpoint", "") or "").strip()
        if not endpoint:
            return
        if not endpoint.lower().startswith(("http://", "https://")):
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
        payload = _payload_for_message(inbound_id)
        if payload is None:
            return

        token = get_setting("system_link_token", "") or ""
        attempt_time = utcnow_iso()
        try:
            _request(payload, endpoint=endpoint, token=token, timeout=timeout)
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
