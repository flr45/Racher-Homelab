from __future__ import annotations

import threading
from collections.abc import Callable

from station_events import (
    active_recipients_for_station,
    mark_first_delivery,
    record_alarm_message,
    station_for_inbound,
)
from storage import add_event, connection, list_recipients, set_message_status, utcnow_iso
from whatsapp_engine import WhatsAppBridgeManager

UpdateCallback = Callable[[int, str], None]


class WhatsAppDeliveryEngine:
    """Deliver accepted inbound SMS messages to active WhatsApp recipients."""

    def __init__(
        self,
        bridge: WhatsAppBridgeManager,
        *,
        poll_seconds: float = 2.0,
        on_update: UpdateCallback | None = None,
    ) -> None:
        self.bridge = bridge
        self.poll_seconds = max(1.0, poll_seconds)
        self.on_update = on_update
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                status = self.bridge.status(timeout=0.7)
                if str(status.get("state")) == "online":
                    self._process_batch()
            except Exception as exc:  # noqa: BLE001
                add_event("error", "whatsapp_delivery_worker", str(exc)[:1000])
            self._stop.wait(self.poll_seconds)

    def _process_batch(self) -> None:
        with connection() as db:
            rows = db.execute(
                """
                SELECT id, sender, body, received_at, processing_status
                FROM inbound_messages
                WHERE accepted=1
                  AND processing_status IN (
                    'accepted_pending_whatsapp',
                    'awaiting_recipients',
                    'whatsapp_sending',
                    'no_matching_recipients'
                  )
                ORDER BY id ASC
                LIMIT 20
                """
            ).fetchall()
            messages = [dict(row) for row in rows]

        for message in messages:
            if self._stop.is_set():
                return
            self._deliver_message(
                int(message["id"]),
                str(message["sender"]),
                str(message["body"]),
                str(message["received_at"]),
            )

    def _deliver_message(
        self,
        message_id: int,
        sender: str,
        body: str,
        received_at: str,
    ) -> None:
        try:
            record_alarm_message(
                message_id,
                sender=sender,
                body=body,
                received_at=received_at,
            )
        except Exception as exc:  # noqa: BLE001
            # Event/statistics enrichment must never block an alarm delivery.
            add_event(
                "error",
                "alarm_event_recording",
                f"SMS {message_id}: {str(exc)[:500]}",
            )

        station = station_for_inbound(message_id)
        recipients = active_recipients_for_station(station)
        if not recipients:
            active_total = len(list_recipients(active_only=True))
            if active_total:
                status = "no_matching_recipients"
                detail = station or "ukendt station"
                add_event(
                    "info",
                    "station_filter_no_match",
                    f"SMS {message_id}: ingen aktive modtagere matcher {detail}",
                )
            else:
                status = "awaiting_recipients"
            set_message_status(message_id, status)
            self._notify(message_id, status)
            return

        set_message_status(message_id, "whatsapp_sending")
        first_success = False
        for recipient in recipients:
            if self._stop.is_set():
                return

            recipient_phone = str(recipient["phone"])
            recipient_name = str(recipient["name"])
            delivery = self._existing_delivery(message_id, recipient_phone)
            if delivery and str(delivery["status"]) in {"sent", "failed"}:
                if str(delivery["status"]) == "sent":
                    first_success = True
                continue

            delivery_id = int(delivery["id"]) if delivery else self._create_delivery(
                message_id,
                recipient_name,
                recipient_phone,
            )

            try:
                whatsapp_message_id = self.bridge.send_text(recipient_phone, body)
                delivered_at = utcnow_iso()
                self._finish_delivery(
                    delivery_id,
                    "sent",
                    message_id=whatsapp_message_id,
                    error=None,
                    attempted_at=delivered_at,
                )
                if not first_success:
                    mark_first_delivery(message_id, delivered_at)
                    first_success = True
                add_event(
                    "info",
                    "whatsapp_sent",
                    f"SMS {message_id} sendt til {recipient_name}"
                    + (f" · station {station}" if station else ""),
                )
            except Exception as exc:  # noqa: BLE001
                self._finish_delivery(
                    delivery_id,
                    "failed",
                    message_id=None,
                    error=str(exc)[:1000],
                    attempted_at=utcnow_iso(),
                )
                add_event(
                    "error",
                    "whatsapp_failed",
                    f"SMS {message_id} til {recipient_name} fejlede: {str(exc)[:300]}",
                )

        sent, failed, pending = self._delivery_summary(message_id)
        if pending:
            final_status = "whatsapp_sending"
        elif failed and sent:
            final_status = "forwarded_partial"
        elif failed:
            final_status = "whatsapp_failed"
        elif sent:
            final_status = "forwarded"
        else:
            final_status = "awaiting_recipients"

        set_message_status(message_id, final_status)
        self._notify(message_id, final_status)

    @staticmethod
    def _existing_delivery(message_id: int, phone: str) -> dict | None:
        with connection() as db:
            row = db.execute(
                """
                SELECT id, status
                FROM whatsapp_deliveries
                WHERE inbound_id=? AND recipient_phone=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (message_id, phone),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _create_delivery(message_id: int, name: str, phone: str) -> int:
        with connection() as db:
            cursor = db.execute(
                """
                INSERT INTO whatsapp_deliveries(
                    inbound_id, recipient_name, recipient_phone,
                    status, message_id, error, attempted_at
                ) VALUES(?,?,?,'pending',NULL,NULL,?)
                """,
                (message_id, name, phone, utcnow_iso()),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _finish_delivery(
        delivery_id: int,
        status: str,
        *,
        message_id: str | None,
        error: str | None,
        attempted_at: str,
    ) -> None:
        with connection() as db:
            db.execute(
                """
                UPDATE whatsapp_deliveries
                SET status=?, message_id=?, error=?, attempted_at=?
                WHERE id=?
                """,
                (status, message_id, error, attempted_at, delivery_id),
            )

    @staticmethod
    def _delivery_summary(message_id: int) -> tuple[int, int, int]:
        with connection() as db:
            row = db.execute(
                """
                SELECT
                  SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
                FROM whatsapp_deliveries
                WHERE inbound_id=?
                """,
                (message_id,),
            ).fetchone()
            return (
                int(row["sent"] or 0),
                int(row["failed"] or 0),
                int(row["pending"] or 0),
            )

    def _notify(self, message_id: int, status: str) -> None:
        if self.on_update:
            self.on_update(message_id, status)


def send_test_to_active_recipients(bridge: WhatsAppBridgeManager) -> tuple[int, int]:
    # A manual test deliberately goes to every active recipient regardless of
    # station filter so the administrator can verify the whole WhatsApp path.
    recipients = list_recipients(active_only=True)
    if not recipients:
        raise RuntimeError("Der er ingen aktive WhatsApp-modtagere")

    text = "SBR Pager Gateway · testbesked"
    sent = 0
    failed = 0
    for recipient in recipients:
        name = str(recipient["name"])
        phone = str(recipient["phone"])
        try:
            message_id = bridge.send_text(phone, text)
            with connection() as db:
                db.execute(
                    """
                    INSERT INTO whatsapp_deliveries(
                        inbound_id, recipient_name, recipient_phone,
                        status, message_id, error, attempted_at
                    ) VALUES(NULL,?,?, 'sent', ?, NULL, ?)
                    """,
                    (name, phone, message_id, utcnow_iso()),
                )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            with connection() as db:
                db.execute(
                    """
                    INSERT INTO whatsapp_deliveries(
                        inbound_id, recipient_name, recipient_phone,
                        status, message_id, error, attempted_at
                    ) VALUES(NULL,?,?, 'failed', NULL, ?, ?)
                    """,
                    (name, phone, str(exc)[:1000], utcnow_iso()),
                )
            failed += 1
    add_event("info", "whatsapp_test", f"Testbesked: {sent} sendt, {failed} fejl")
    return sent, failed
