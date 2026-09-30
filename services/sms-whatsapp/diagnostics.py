"""Independent WAN diagnostics and durable, explicitly selected WhatsApp tests."""
import os
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import delivery_retry_app as deliveries

base = deliveries.base
app = base.app
db = base.db
_internet_lock = threading.Lock()
_internet_cache = (0.0, {})


def probe_internet():
    urls = os.getenv("SMS_WHATSAPP_INTERNET_CHECK_URLS", "https://www.gstatic.com/generate_204,https://www.cloudflare.com/cdn-cgi/trace").split(",")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    errors = []
    reached = False
    attempted = 0
    for url in urls[:2]:
        url = url.strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            errors.append("Kontroladressen skal være HTTPS uden loginoplysninger")
            continue
        try:
            attempted += 1
            with opener.open(urllib.request.Request(url, method="HEAD"), timeout=2) as response:
                target = urllib.parse.urlsplit(response.geturl())
                reached = True
                if response.status in {200, 204} and (target.scheme, target.netloc) == (parsed.scheme, parsed.netloc):
                    return {"state": "online", "detail": "DNS og HTTPS fra Pager er bekræftet", "checked_at": base.utcnow()}
                errors.append("Kontrolserveren gav et uventet svar eller omdirigerede forbindelsen")
        except urllib.error.HTTPError:
            reached = True
            errors.append("Kontrolserveren svarede med en HTTP-fejl")
        except (urllib.error.URLError, OSError) as exc:
            cause = getattr(exc, "reason", exc)
            errors.append("DNS-opslag fejlede" if isinstance(cause, socket.gaierror) else
                          "TLS-certifikatet kunne ikke bekræftes" if isinstance(cause, ssl.SSLError) else
                          "HTTPS-kontrollen kunne ikke nå kontrolserveren")
    return {"state": "unknown" if not attempted else "degraded" if reached else "offline", "detail": "; ".join(dict.fromkeys(errors)) or "Ingen kontroladresse er konfigureret",
            "checked_at": base.utcnow()}


def internet_status():
    global _internet_cache
    with _internet_lock:
        if time.monotonic() - _internet_cache[0] >= 30 or not _internet_cache[1]:
            _internet_cache = (time.monotonic(), probe_internet())
        return dict(_internet_cache[1])


class SingleWhatsAppTest(db.Model):
    __tablename__ = "single_whatsapp_test"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False, unique=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("recipient.id", ondelete="SET NULL"))
    delivery_id = db.Column(db.Integer, db.ForeignKey("whats_app_delivery.id", ondelete="SET NULL"))
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=base.utcnow)
    started_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    elapsed_ms = db.Column(db.Integer)
    error = db.Column(db.Text)


def enqueue_single_test(recipient, token):
    # Same lock as normal ingest; a double-click must create only one job.
    with base.ingest_lock:
        old = SingleWhatsAppTest.query.filter_by(token=token).first()
        if old:
            return old
        row = base.WhatsAppDelivery(inbound_id=None, recipient_name=recipient.name,
            recipient_phone=recipient.phone, status="pending", attempted_at=base.utcnow())
        db.session.add(row)
        db.session.flush()
        test = SingleWhatsAppTest(token=token, recipient_id=recipient.id, delivery_id=row.id, status="pending")
        db.session.add(test)
        db.session.commit()
        deliveries._wake_event.set()
        return test


def recover_inflight_tests():
    # The network may have accepted a test before a process died. Do not send
    # it a second time automatically: record the uncertain outcome instead.
    for test in SingleWhatsAppTest.query.filter_by(status="running").all():
        test.status = "uncertain"
        test.error = "Programmet genstartede under afsendelsen. Kontrollér telefonen før en ny test."
        test.completed_at = base.utcnow()
        row = db.session.get(base.WhatsAppDelivery, test.delivery_id) if test.delivery_id else None
        if row:
            row.status = "failed"
            row.error = test.error
    db.session.commit()


@deliveries.serialized
def run_single_test():
    paused = getattr(deliveries, "operations_paused", None)
    if paused and paused():
        return
    test = SingleWhatsAppTest.query.filter_by(status="pending").order_by(SingleWhatsAppTest.id).first()
    if not test:
        return
    row = db.session.get(base.WhatsAppDelivery, test.delivery_id) if test.delivery_id else None
    recipient = db.session.get(base.Recipient, test.recipient_id) if test.recipient_id else None
    if not row or not recipient or not recipient.active or recipient.phone != row.recipient_phone:
        test.status = "cancelled"
        test.error = "Modtageren er pauset, ændret eller slettet"
    elif (base.utcnow() - deliveries._aware(test.created_at)).total_seconds() > 300:
        test.status = "cancelled"
        test.error = "Testen udløb efter fem minutter i køen"
    else:
        test.status = "running"
        test.started_at = base.utcnow()
        db.session.commit()
        started = time.monotonic()
        try:
            row.message_id = base.send_whatsapp(row.recipient_phone, f"✅ SBR Pager · forbindelsestest #{test.id}\nKun sendt til det valgte nummer.")
            test.status = "sent"
        except Exception as exc:
            test.status = "failed"
            test.error = str(exc)[:1000]
        test.elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    test.completed_at = base.utcnow()
    if row:
        row.status = test.status
        row.error = test.error
        row.attempted_at = test.started_at or test.completed_at
    db.session.commit()


def test_details(test):
    row = db.session.get(base.WhatsAppDelivery, test.delivery_id) if test.delivery_id else None
    return {"id": test.id, "status": test.status, "elapsed_ms": test.elapsed_ms, "error": test.error,
            "message_id": row.message_id if row else None, "phone": row.recipient_phone if row else "—",
            "name": row.recipient_name if row else "Slettet levering", "created_at": test.created_at}


def explain_inbound(inbound):
    decision = deliveries.InboundDecision.query.filter_by(inbound_id=inbound.id).first()
    rows = base.WhatsAppDelivery.query.filter_by(inbound_id=inbound.id).all()
    if decision and decision.decision != "deliver":
        return decision.reason or "Beskeden blev stoppet før udsendelse"
    if not inbound.accepted:
        return "Afvist ved modtagelse; den præcise årsag blev ikke gemt i den tidligere version"
    if not rows:
        return "Ingen leveringsjobs registreret; ældre beskeder kan mangle en registreret årsag"
    counts = {key: sum(row.status == key for row in rows) for key in {row.status for row in rows}}
    labels = {"sent": "kvitteret af OpenWA", "pending": "venter i kø", "retrying": "venter på nyt forsøg", "failed": "fejlet", "cancelled": "annulleret", "held": "afventer godkendelse af gammel alarm"}
    return ", ".join(f"{count} {labels.get(key, key)}" for key, count in sorted(counts.items()))


with app.app_context():
    db.create_all()
    if deliveries.RETRY_WORKER_ENABLED:
        recover_inflight_tests()

# Share the existing single-process network worker; no additional sending
# process, and normal alarm jobs retain priority over administrator tests.
deliveries.manual_test_runner = run_single_test
