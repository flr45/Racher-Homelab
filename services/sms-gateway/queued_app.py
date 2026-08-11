"""Runtime wrapper for the SMS gateway.

The original web application remains in ``base_app.py``. This wrapper adds a
persistent outgoing queue, an authenticated SMS command queue and replaces
direct serial access in the web process. Only ``modem_reader.py`` is allowed
to own the modem device.
"""

import contextvars
import json
import logging
import os
import time
import urllib.error
import urllib.request
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


class CommandRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(20), nullable=False, index=True)
    command = db.Column(db.String(40), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    error = db.Column(db.Text)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)


_active_outbound_messages = contextvars.ContextVar(
    "sms_active_outbound_messages",
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


def send_sms(recipient: str, body: str):
    """Queue an SMS; inbound forwarding is asynchronous, CLI calls wait."""
    queued_messages = _active_outbound_messages.get()

    if queued_messages is not None:
        normalized = base.normalize_phone(recipient)
        message = enqueue_sms(normalized, body)
        queued_messages.append((message.id, normalized))
        log.info("SMS %s sat i kø til %s", message.id, normalized)
        return

    wait_seconds = max(5, int(os.getenv("SMS_SEND_WAIT_SECONDS", "40")))
    api_base = os.getenv(
        "GATEWAY_API_BASE_URL",
        "http://127.0.0.1:8080",
    ).rstrip("/")
    payload = json.dumps(
        {"recipient": recipient, "body": body},
        ensure_ascii=False,
    ).encode("utf-8")
    outgoing = urllib.request.Request(
        f"{api_base}/api/outgoing",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(outgoing, timeout=10) as response:
            queued = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"SMS-køen svarede HTTP {exc.code}: {details[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Kunne ikke kontakte SMS-køen: {exc.reason}") from exc

    message_id = queued["id"]
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{api_base}/api/outgoing/{message_id}",
                timeout=8,
            ) as response:
                current = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"SMS-status svarede HTTP {exc.code}: {details[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Kunne ikke kontakte SMS-status: {exc.reason}"
            ) from exc

        if current["status"] == "sent":
            return
        if current["status"] == "failed":
            raise RuntimeError(current.get("error") or "SMS-afsendelsen fejlede")
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


def command_name(body: str) -> str | None:
    normalized = " ".join((body or "").strip().casefold().split())
    aliases = {
        "status": "status",
        "server status": "status",
        "serverstatus": "status",
    }
    return aliases.get(normalized)


def allowed_command_senders() -> set[str]:
    values = []
    for key in ("SMS_COMMAND_ALLOWED_NUMBERS", "RACHER_MONITOR_SMS_TO"):
        values.extend(os.getenv(key, "").split(","))

    result: set[str] = set()
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            continue
        try:
            result.add(base.normalize_phone(value))
        except ValueError:
            log.warning("Ignorerer ugyldigt SMS-kommandonummer fra miljøvariabel")
    return result


def enqueue_command(sender: str, command: str):
    request_row = CommandRequest(
        sender=base.normalize_phone(sender),
        command=command,
        status="pending",
        created_at=utcnow(),
    )
    db.session.add(request_row)
    db.session.commit()
    return request_row


def cleanup_commands():
    retention_days = max(1, int(os.getenv("SMS_COMMAND_RETENTION_DAYS", "7")))
    cutoff = utcnow() - timedelta(days=retention_days)
    deleted = CommandRequest.query.filter(
        CommandRequest.status.in_({"done", "failed"}),
        CommandRequest.completed_at.is_not(None),
        CommandRequest.completed_at < cutoff,
    ).delete(synchronize_session=False)
    if deleted:
        db.session.commit()


def claim_command_request():
    cleanup_commands()
    stale_seconds = max(30, int(os.getenv("SMS_COMMAND_STALE_SECONDS", "120")))
    stale_before = utcnow() - timedelta(seconds=stale_seconds)

    message = (
        CommandRequest.query.filter_by(status="pending")
        .order_by(CommandRequest.created_at.asc(), CommandRequest.id.asc())
        .first()
    )
    if message is None:
        message = (
            CommandRequest.query.filter(
                CommandRequest.status == "processing",
                CommandRequest.claimed_at.is_not(None),
                CommandRequest.claimed_at < stale_before,
            )
            .order_by(CommandRequest.claimed_at.asc())
            .first()
        )
    if message is None:
        return None

    message.status = "processing"
    message.claimed_at = utcnow()
    message.completed_at = None
    message.error = None
    message.attempts = int(message.attempts or 0) + 1
    db.session.commit()
    return message


def complete_command_request(
    message: CommandRequest,
    status: str,
    error: str | None = None,
    retry: bool = False,
):
    max_attempts = max(1, int(os.getenv("SMS_COMMAND_MAX_ATTEMPTS", "3")))
    should_retry = retry and int(message.attempts or 0) < max_attempts
    if should_retry:
        message.status = "pending"
        message.claimed_at = None
        message.completed_at = None
        message.error = (error or "SMS-kommandoen fejlede")[:1000]
    else:
        message.status = status
        message.completed_at = utcnow()
        message.error = (error or "")[:1000] or None
    db.session.commit()
    return should_retry


_original_process_incoming = base.process_incoming


def process_incoming(*args, **kwargs):
    sender = kwargs.get("sender", args[0] if len(args) >= 1 else "")
    body = kwargs.get("body", args[1] if len(args) >= 2 else "")
    received_at = kwargs.get("received_at", args[2] if len(args) >= 3 else None)

    normalized_sender = base.normalize_phone(sender)
    detected_command = command_name(body)
    if detected_command and normalized_sender in allowed_command_senders():
        parsed_received_at = base.parse_received_at(received_at)
        inbound = base.InboundMessage(
            sender=normalized_sender,
            body=(body or "").strip(),
            station_code=None,
            received_at=parsed_received_at,
        )
        db.session.add(inbound)
        db.session.flush()
        command = CommandRequest(
            sender=normalized_sender,
            command=detected_command,
            status="pending",
            created_at=utcnow(),
        )
        db.session.add(command)
        db.session.commit()
        base.write_gateway_status(
            state="online",
            database="online",
            last_received_sms_at=parsed_received_at.isoformat(),
            last_error=None,
        )
        log.info(
            "SMS-kommando %s fra %s sat i kø som %s",
            detected_command,
            normalized_sender,
            command.id,
        )
        return inbound, 0, {
            "created": False,
            "alarmId": None,
            "sequenceNumber": None,
        }

    queued_messages: list[tuple[int, str]] = []
    token = _active_outbound_messages.set(queued_messages)
    try:
        result = _original_process_incoming(*args, **kwargs)
    finally:
        _active_outbound_messages.reset(token)

    # base_app marks a successful direct call as "sent". At runtime the call
    # only queued the SMS, so connect each queue row to the exact inbound
    # delivery and correct its state until the modem worker records the result.
    inbound = result[0]
    for message_id, recipient in queued_messages:
        delivery = (
            base.Delivery.query.filter_by(
                inbound_id=inbound.id,
                recipient=recipient,
            )
            .order_by(base.Delivery.id.desc())
            .first()
        )
        message = db.session.get(OutboundMessage, message_id)
        if delivery and message:
            message.delivery_id = delivery.id
            delivery.status = "queued"
            delivery.error = None
    if queued_messages:
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


@app.post("/api/commands/claim")
def claim_command():
    command = claim_command_request()
    if command is None:
        return "", 204
    return jsonify(
        id=command.id,
        sender=command.sender,
        command=command.command,
        status=command.status,
        attempts=command.attempts,
    )


@app.post("/api/commands/<int:command_id>/complete")
def command_complete(command_id):
    command = db.get_or_404(CommandRequest, command_id)
    payload = request.get_json(force=True)
    status = str(payload.get("status", "")).lower()
    if status not in {"done", "failed"}:
        return jsonify(error="Status skal være done eller failed"), 400

    retried = complete_command_request(
        command,
        status=status,
        error=payload.get("error"),
        retry=bool(payload.get("retry", False)),
    )
    return jsonify(
        id=command.id,
        status=command.status,
        attempts=command.attempts,
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
        payload["gateway"]["commands_pending"] = CommandRequest.query.filter(
            CommandRequest.status.in_({"pending", "processing"})
        ).count()
        payload["gateway"]["commands_failed"] = (
            CommandRequest.query.filter_by(status="failed").count()
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        payload["gateway"]["outbox_error"] = str(exc)[:500]
        payload["status"] = "degraded"
    return jsonify(payload)


app.view_functions["health"] = health_with_outbox


with app.app_context():
    db.create_all()
