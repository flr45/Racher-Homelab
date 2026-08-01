"""Runtime wrapper for the SMS gateway.

The original web application remains in ``base_app.py``. This wrapper adds a
persistent outgoing queue and replaces direct serial access in the web process.
Only ``modem_reader.py`` is allowed to own the modem device.
"""

import contextvars
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import base_app as base
from flask import jsonify, request

app = base.app
db = base.db
log = logging.getLogger("sms-gateway-outbox")

# Re-export the public helpers used by build checks and maintenance commands.
detect_station_code = base.detect_station_code
normalize_phone = base.normalize_phone


class OutboundMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(20), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    error = db.Column(db.Text)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    delivery_id = db.Column(
        db.Integer,
        db.ForeignKey("delivery.id"),
        nullable=True,
        index=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)


_active_delivery_ids = contextvars.ContextVar(
    "sms_active_delivery_ids",
    default=None,
)


def utcnow():
    return datetime.now(timezone.utc)


def enqueue_sms(recipient: str, body: str, delivery_id: int | None = None):
    recipient = base.normalize_phone(recipient)
    body = (body or "").strip()
    if not body:
        raise ValueError("SMS-teksten er tom")

    message = OutboundMessage(
        recipient=recipient,
        body=body,
        status="pending",
        delivery_id=delivery_id,
        created_at=utcnow(),
    )
    db.session.add(message)
    db.session.commit()
    return message


def _current_pending_delivery(recipient: str):
    return (
        base.Delivery.query.filter_by(recipient=recipient, status="pending")
        .order_by(base.Delivery.id.desc())
        .first()
    )


def send_sms(recipient: str, body: str):
    """Queue an SMS; inbound forwarding is asynchronous, CLI calls wait."""
    delivery_ids = _active_delivery_ids.get()

    if delivery_ids is not None:
        normalized = base.normalize_phone(recipient)
        delivery = _current_pending_delivery(normalized)
        message = enqueue_sms(
            normalized,
            body,
            delivery_id=delivery.id if delivery else None,
        )
        if delivery:
            delivery_ids.append(delivery.id)
        log.info("SMS %s sat i kø til %s", message.id, normalized)
        return

    wait_seconds = max(5, int(os.getenv("SMS_SEND_WAIT_SECONDS", "40")))
    with app.app_context():
        message = enqueue_sms(recipient, body)
        message_id = message.id

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        with app.app_context():
            db.session.remove()
            current = db.session.get(OutboundMessage, message_id)
            if current is None:
                raise RuntimeError("SMS-køelementet forsvandt")
            if current.status == "sent":
                return
            if current.status == "failed":
                raise RuntimeError(current.error or "SMS-afsendelsen fejlede")
        time.sleep(0.5)

    raise TimeoutError("SMS-afsendelsen blev ikke færdig inden timeout")


def cleanup_outbox():
    retention_days = max(1, int(os.getenv("SMS_OUTBOX_RETENTION_DAYS", "7")))
    cutoff = utcnow() - timedelta(days=retention_days)
    deleted = OutboundMessage.query.filter(
        OutboundMessage.status.in_({"sent", "failed"}),
        OutboundMessage.completed_at.is_not(None),
        OutboundMessage.completed_at < cutoff,
    ).delete(synchronize_session=False)
    if deleted:
        db.session.commit()


def claim_outbound_message():
    cleanup_outbox()
    stale_seconds = max(30, int(os.getenv("SMS_OUTBOX_STALE_SECONDS", "120")))
    stale_before = utcnow() - timedelta(seconds=stale_seconds)

    message = (
        OutboundMessage.query.filter_by(status="pending")
        .order_by(OutboundMessage.created_at.asc(), OutboundMessage.id.asc())
        .first()
    )
    if message is None:
        message = (
            OutboundMessage.query.filter(
                OutboundMessage.status == "sending",
                OutboundMessage.claimed_at.is_not(None),
                OutboundMessage.claimed_at < stale_before,
            )
            .order_by(OutboundMessage.claimed_at.asc())
            .first()
        )
    if message is None:
        return None

    message.status = "sending"
    message.claimed_at = utcnow()
    message.completed_at = None
    message.error = None
    message.attempts = int(message.attempts or 0) + 1
    db.session.commit()
    return message


def complete_outbound_message(
    message: OutboundMessage,
    status: str,
    error: str | None = None,
    retry: bool = False,
):
    max_attempts = max(1, int(os.getenv("SMS_OUTBOX_MAX_ATTEMPTS", "3")))
    should_retry = retry and int(message.attempts or 0) < max_attempts
    now = utcnow()

    if should_retry:
        message.status = "pending"
        message.claimed_at = None
        message.completed_at = None
        message.error = (error or "SMS-afsendelsen fejlede")[:1000]
        delivery_status = "queued"
    else:
        message.status = status
        message.completed_at = now
        message.error = (error or "")[:1000] or None
        delivery_status = status

    if message.delivery_id:
        delivery = db.session.get(base.Delivery, message.delivery_id)
        if delivery:
            delivery.status = delivery_status
            delivery.error = message.error
            delivery.attempted_at = now

    db.session.commit()
    return should_retry


_original_process_incoming = base.process_incoming


def process_incoming(*args, **kwargs):
    delivery_ids: list[int] = []
    token = _active_delivery_ids.set(delivery_ids)
    try:
        result = _original_process_incoming(*args, **kwargs)
    finally:
        _active_delivery_ids.reset(token)

    # base_app marks a successful direct call as "sent". At runtime the call
    # only queued the SMS, so correct the delivery state until the modem worker
    # records the final result.
    for delivery_id in delivery_ids:
        delivery = db.session.get(base.Delivery, delivery_id)
        if delivery:
            delivery.status = "queued"
            delivery.error = None
    if delivery_ids:
        db.session.commit()
    return result


# Patch the original module because its already-registered Flask views resolve
# these globals from base_app at request time.
base.send_sms = send_sms
base.process_incoming = process_incoming


@app.post("/api/outgoing")
def outgoing():
    payload = request.get_json(force=True)
    try:
        message = enqueue_sms(
            payload.get("recipient", ""),
            payload.get("body", ""),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=message.id, status=message.status), 202


@app.post("/api/outgoing/claim")
def claim_outgoing():
    message = claim_outbound_message()
    if message is None:
        return "", 204
    return jsonify(
        id=message.id,
        recipient=message.recipient,
        body=message.body,
        status=message.status,
        attempts=message.attempts,
    )


@app.get("/api/outgoing/<int:message_id>")
def outgoing_status(message_id):
    message = db.get_or_404(OutboundMessage, message_id)
    return jsonify(
        id=message.id,
        status=message.status,
        error=message.error,
        attempts=message.attempts,
        created_at=message.created_at.isoformat() if message.created_at else None,
        claimed_at=message.claimed_at.isoformat() if message.claimed_at else None,
        completed_at=(
            message.completed_at.isoformat() if message.completed_at else None
        ),
    )


@app.post("/api/outgoing/<int:message_id>/complete")
def outgoing_complete(message_id):
    message = db.get_or_404(OutboundMessage, message_id)
    payload = request.get_json(force=True)
    status = str(payload.get("status", "")).lower()
    if status not in {"sent", "failed"}:
        return jsonify(error="Status skal være sent eller failed"), 400

    retried = complete_outbound_message(
        message,
        status=status,
        error=payload.get("error"),
        retry=bool(payload.get("retry", False)),
    )
    return jsonify(
        id=message.id,
        status=message.status,
        attempts=message.attempts,
        retried=retried,
    )


_original_health = app.view_functions["health"]


def health_with_outbox():
    response = _original_health()
    payload = response.get_json()
    try:
        payload["gateway"]["outbox_pending"] = OutboundMessage.query.filter(
            OutboundMessage.status.in_({"pending", "sending"})
        ).count()
        payload["gateway"]["outbox_failed"] = (
            OutboundMessage.query.filter_by(status="failed").count()
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        payload["gateway"]["outbox_error"] = str(exc)[:500]
        payload["status"] = "degraded"
    return jsonify(payload)


app.view_functions["health"] = health_with_outbox


with app.app_context():
    db.create_all()
