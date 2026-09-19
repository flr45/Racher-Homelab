from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from flask import flash, jsonify, redirect, render_template_string, request, url_for

import geocode_app as previous
import station_filter_app as stations

app = previous.app
base = stations.base
db = base.db
log = logging.getLogger("sbr-pager-delivery-retry")

RETRY_ENABLED = os.getenv("SMS_WHATSAPP_RETRY_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
RETRY_WORKER_ENABLED = os.getenv("SMS_WHATSAPP_RETRY_WORKER", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
RETRY_BASE_SECONDS = max(5, int(os.getenv("SMS_WHATSAPP_RETRY_BASE_SECONDS", "15")))
RETRY_MAX_SECONDS = max(RETRY_BASE_SECONDS, int(os.getenv("SMS_WHATSAPP_RETRY_MAX_SECONDS", "300")))
RETRY_MAX_ATTEMPTS = max(1, int(os.getenv("SMS_WHATSAPP_RETRY_MAX_ATTEMPTS", "20")))
RETRY_MAX_AGE_HOURS = max(1, int(os.getenv("SMS_WHATSAPP_RETRY_MAX_AGE_HOURS", "24")))
RETRY_POLL_SECONDS = max(2, int(os.getenv("SMS_WHATSAPP_RETRY_POLL_SECONDS", "5")))


class WhatsAppRetryState(db.Model):
    __tablename__ = "whatsapp_retry_state"

    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(
        db.Integer,
        db.ForeignKey("whats_app_delivery.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    attempts = db.Column(db.Integer, nullable=False, default=0)
    first_failed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=base.utcnow)
    next_attempt_at = db.Column(db.DateTime(timezone=True), index=True)
    last_error = db.Column(db.Text)
    completed_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=base.utcnow)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _retry_delay(attempts: int) -> int:
    exponent = max(0, min(attempts - 1, 10))
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** exponent))


def _state_for(delivery_id: int) -> WhatsAppRetryState | None:
    return WhatsAppRetryState.query.filter_by(delivery_id=delivery_id).first()


def _queue_failure(delivery: base.WhatsAppDelivery, error: Exception | str) -> None:
    now = base.utcnow()
    state = _state_for(delivery.id)
    if state is None:
        state = WhatsAppRetryState(
            delivery_id=delivery.id,
            attempts=0,
            first_failed_at=now,
            updated_at=now,
        )
        db.session.add(state)

    state.attempts = int(state.attempts or 0) + 1
    state.last_error = str(error)[:1000]
    state.updated_at = now
    delivery.error = state.last_error
    delivery.attempted_at = now

    first_failed = _aware(state.first_failed_at) or now
    too_old = (now - first_failed).total_seconds() >= RETRY_MAX_AGE_HOURS * 3600
    exhausted = state.attempts >= RETRY_MAX_ATTEMPTS

    if not RETRY_ENABLED or too_old or exhausted:
        delivery.status = "failed"
        state.next_attempt_at = None
        state.completed_at = now
        return

    delivery.status = "retrying"
    state.next_attempt_at = now + timedelta(seconds=_retry_delay(state.attempts))
    state.completed_at = None


def _mark_sent(
    delivery: base.WhatsAppDelivery,
    message_id: str | None,
    state: WhatsAppRetryState | None,
) -> None:
    now = base.utcnow()
    delivery.message_id = message_id
    delivery.status = "sent"
    delivery.error = None
    delivery.attempted_at = now
    if state is not None:
        state.last_error = None
        state.next_attempt_at = None
        state.completed_at = now
        state.updated_at = now


def attempt_delivery(
    delivery: base.WhatsAppDelivery,
    inbound: base.InboundMessage,
) -> bool:
    state = _state_for(delivery.id)
    try:
        message_id = base.send_whatsapp(delivery.recipient_phone, inbound.body)
        _mark_sent(delivery, message_id, state)
        db.session.commit()
        if delivery.inbound_id is not None:
            stations.events.mark_first_delivery(delivery.inbound_id, delivery.attempted_at)
        return True
    except Exception as exc:  # noqa: BLE001
        _queue_failure(delivery, exc)
        db.session.commit()
        log.warning(
            "WhatsApp-levering %s til %s fejlede (status=%s): %s",
            delivery.id,
            delivery.recipient_phone,
            delivery.status,
            exc,
        )
        return False


def deliver_inbound_resilient(inbound: base.InboundMessage) -> tuple[int, int]:
    """Station-filtered delivery with durable retry state.

    The first OpenWA attempt still happens immediately. If it fails, the same
    delivery row is kept and retried in the background instead of the alarm
    being abandoned after one request.
    """

    station = stations.station_for_inbound(inbound)
    recipients = base.Recipient.query.filter_by(active=True).order_by(base.Recipient.name).all()
    sent = 0
    queued_or_failed = 0

    for recipient in recipients:
        if not stations.recipient_accepts(recipient.id, station):
            continue

        existing = (
            base.WhatsAppDelivery.query.filter_by(
                inbound_id=inbound.id,
                recipient_phone=recipient.phone,
            )
            .order_by(base.WhatsAppDelivery.id.desc())
            .first()
        )
        if existing is not None:
            if existing.status == "sent":
                sent += 1
            else:
                queued_or_failed += 1
            continue

        delivery = base.WhatsAppDelivery(
            inbound_id=inbound.id,
            recipient_name=recipient.name,
            recipient_phone=recipient.phone,
            status="pending",
            attempted_at=base.utcnow(),
        )
        db.session.add(delivery)
        db.session.commit()

        if attempt_delivery(delivery, inbound):
            sent += 1
        else:
            queued_or_failed += 1

    return sent, queued_or_failed


# app.py resolves this global at request time, so the complete events/geocode
# wrapper chain automatically uses the resilient delivery implementation.
base.deliver_inbound = deliver_inbound_resilient


def _seed_recent_legacy_failures() -> int:
    """Adopt recent failed rows created before this retry module existed."""
    if not RETRY_ENABLED:
        return 0
    cutoff = base.utcnow() - timedelta(hours=RETRY_MAX_AGE_HOURS)
    failures = (
        base.WhatsAppDelivery.query.filter(
            base.WhatsAppDelivery.status == "failed",
            base.WhatsAppDelivery.inbound_id.is_not(None),
            base.WhatsAppDelivery.attempted_at >= cutoff,
        )
        .order_by(base.WhatsAppDelivery.id.asc())
        .limit(100)
        .all()
    )
    created = 0
    for delivery in failures:
        if _state_for(delivery.id) is not None:
            continue
        state = WhatsAppRetryState(
            delivery_id=delivery.id,
            attempts=0,
            first_failed_at=delivery.attempted_at or base.utcnow(),
            next_attempt_at=base.utcnow(),
            last_error=delivery.error,
            updated_at=base.utcnow(),
        )
        delivery.status = "retrying"
        db.session.add(state)
        created += 1
    if created:
        db.session.commit()
    return created


def retry_due_once(limit: int = 20) -> dict:
    if not RETRY_ENABLED:
        return {"checked": 0, "sent": 0, "remaining": 0}

    _seed_recent_legacy_failures()
    now = base.utcnow()
    rows = (
        db.session.query(WhatsAppRetryState, base.WhatsAppDelivery, base.InboundMessage)
        .join(base.WhatsAppDelivery, base.WhatsAppDelivery.id == WhatsAppRetryState.delivery_id)
        .join(base.InboundMessage, base.InboundMessage.id == base.WhatsAppDelivery.inbound_id)
        .filter(
            WhatsAppRetryState.completed_at.is_(None),
            WhatsAppRetryState.next_attempt_at.is_not(None),
            WhatsAppRetryState.next_attempt_at <= now,
            base.WhatsAppDelivery.status.in_(("retrying", "pending")),
        )
        .order_by(WhatsAppRetryState.next_attempt_at.asc(), WhatsAppRetryState.id.asc())
        .limit(max(1, int(limit)))
        .all()
    )

    sent = 0
    for _state, delivery, inbound in rows:
        if attempt_delivery(delivery, inbound):
            sent += 1

    remaining = (
        WhatsAppRetryState.query.filter(
            WhatsAppRetryState.completed_at.is_(None),
            WhatsAppRetryState.next_attempt_at.is_not(None),
        ).count()
    )
    return {"checked": len(rows), "sent": sent, "remaining": remaining}


def queue_snapshot() -> dict:
    retrying = base.WhatsAppDelivery.query.filter_by(status="retrying").count()
    pending = base.WhatsAppDelivery.query.filter_by(status="pending").count()
    failed = base.WhatsAppDelivery.query.filter_by(status="failed").count()
    active = WhatsAppRetryState.query.filter(WhatsAppRetryState.completed_at.is_(None)).count()

    oldest = (
        WhatsAppRetryState.query.filter(WhatsAppRetryState.completed_at.is_(None))
        .order_by(WhatsAppRetryState.first_failed_at.asc())
        .first()
    )
    oldest_minutes = None
    if oldest is not None:
        first = _aware(oldest.first_failed_at)
        if first is not None:
            oldest_minutes = max(0, int((base.utcnow() - first).total_seconds() // 60))

    return {
        "active": active,
        "retrying": retrying,
        "pending": pending,
        "failed": failed,
        "oldestMinutes": oldest_minutes,
        "maxAttempts": RETRY_MAX_ATTEMPTS,
        "maxAgeHours": RETRY_MAX_AGE_HOURS,
    }


_stop_event = threading.Event()
_worker_lock = threading.Lock()
_worker_started = False


def _retry_worker() -> None:
    log.info(
        "WhatsApp retry-worker startet: base=%ss max=%ss attempts=%s age=%sh",
        RETRY_BASE_SECONDS,
        RETRY_MAX_SECONDS,
        RETRY_MAX_ATTEMPTS,
        RETRY_MAX_AGE_HOURS,
    )
    while not _stop_event.is_set():
        try:
            with app.app_context():
                retry_due_once()
        except Exception:  # noqa: BLE001
            log.exception("WhatsApp retry-worker fejlede")
        _stop_event.wait(RETRY_POLL_SECONDS)


def start_retry_worker() -> None:
    global _worker_started
    if not RETRY_ENABLED or not RETRY_WORKER_ENABLED:
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(
            target=_retry_worker,
            name="sbr-whatsapp-retry",
            daemon=True,
        )
        thread.start()
        _worker_started = True


RETRY_FRAGMENT = r"""
<section class="card span12" id="delivery-safety">
  <div class="top" style="margin-bottom:10px">
    <div>
      <h2 style="margin:0">Leveringssikkerhed</h2>
      <p class="muted" style="margin:5px 0 0">OpenWA-fejl køes og forsøges automatisk igen – også efter container-genstart.</p>
    </div>
    <form method="post" action="{{ url_for('retry_whatsapp_deliveries_now') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn small" type="submit">Genforsøg nu</button>
    </form>
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px">
    <div><div class="metric">{{ queue.active }}</div><div class="muted">I retry-kø</div></div>
    <div><div class="metric">{{ queue.retrying }}</div><div class="muted">Afventer nyt forsøg</div></div>
    <div><div class="metric">{{ queue.failed }}</div><div class="muted">Permanent fejlet</div></div>
    <div><div class="metric">{{ (queue.oldestMinutes|string + ' min') if queue.oldestMinutes is not none else '—' }}</div><div class="muted">Ældste aktive fejl</div></div>
  </div>
</section>
"""


_previous_dashboard = app.view_functions["dashboard"]


def dashboard_with_delivery_safety():
    raw_response = _previous_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response

    html = response.get_data(as_text=True)
    if 'id="delivery-safety"' not in html:
        fragment = render_template_string(RETRY_FRAGMENT, queue=queue_snapshot())
        markers = [
            '<section class="card span12"><h2>Teknisk WhatsApp-log</h2>',
            '<section class="card span12"><h2>Teknisk SMS-log</h2>',
            '<div class="footer">',
        ]
        for marker in markers:
            if marker in html:
                html = html.replace(marker, fragment + marker, 1)
                break
    response.set_data(html)
    return response


app.view_functions["dashboard"] = dashboard_with_delivery_safety


_previous_health = app.view_functions["health"]


def health_with_delivery_queue():
    response = app.make_response(_previous_health())
    data = response.get_json(silent=True) or {}
    data["deliveryQueue"] = queue_snapshot()
    return jsonify(data), response.status_code


app.view_functions["health"] = health_with_delivery_queue


@app.post("/leveringsko/genforsog")
@base.login_required
def retry_whatsapp_deliveries_now():
    base.check_csrf()
    now = base.utcnow()
    cutoff = now - timedelta(hours=RETRY_MAX_AGE_HOURS)

    failed = (
        base.WhatsAppDelivery.query.filter(
            base.WhatsAppDelivery.status == "failed",
            base.WhatsAppDelivery.inbound_id.is_not(None),
            base.WhatsAppDelivery.attempted_at >= cutoff,
        )
        .order_by(base.WhatsAppDelivery.id.asc())
        .limit(100)
        .all()
    )
    for delivery in failed:
        state = _state_for(delivery.id)
        if state is None:
            state = WhatsAppRetryState(
                delivery_id=delivery.id,
                attempts=0,
                first_failed_at=now,
                updated_at=now,
            )
            db.session.add(state)
        state.attempts = 0
        state.first_failed_at = now
        state.next_attempt_at = now
        state.completed_at = None
        state.updated_at = now
        delivery.status = "retrying"
    db.session.commit()

    result = retry_due_once(limit=100)
    flash(
        f"Leveringskø behandlet: {result['sent']} sendt nu, "
        f"{result['remaining']} afventer fortsat."
    )
    return redirect(request.referrer or url_for("dashboard"))


with app.app_context():
    db.create_all()

start_retry_worker()
