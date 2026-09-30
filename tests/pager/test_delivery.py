from datetime import timedelta
import threading

import pytest

HEADERS = {"Authorization": "Bearer test-ingest"}


def ingest(p, source="alarm-1", body="(S)M+V · Bygn.brand · Eksempelvej 12, 4180 Sorø", **metadata):
    payload = dict(sender="+4512345678", body=body, receivedAt="2026-09-30T20:00:00Z", sourceMessageId=source, **metadata)
    return p.app.test_client().post("/api/incoming", json=payload, headers=HEADERS)


def test_modem_acknowledged_without_openwa_network_call(authorized, monkeypatch):
    p = authorized
    calls = []
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: calls.append(args) or "wa-ok")
    for n in range(30):
        p.db.session.add(p.base.Recipient(name=f"Bruger {n}", phone=f"+453333{n:04}", active=True))
    p.db.session.commit()
    response = ingest(p)
    assert response.status_code == 201
    assert response.json["queued"] == 31
    assert response.json["failed"] == 0 and not calls
    assert p.deliveries.WhatsAppRetryState.query.count() == 31
    assert p.deliveries.retry_due_once(limit=100)["sent"] == 31
    assert len(calls) == 31


def test_slow_worker_does_not_block_a_new_sms(authorized, monkeypatch):
    p = authorized
    started, release = threading.Event(), threading.Event()
    errors = []
    def slow_send(*args):
        started.set()
        assert release.wait(5)
        return "wa-ok"
    monkeypatch.setattr(p.base, "send_whatsapp", slow_send)
    ingest(p, "first")
    def worker():
        try:
            with p.app.app_context():p.deliveries.retry_due_once()
        except Exception as exc:errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(2)
        assert ingest(p, "second").json["queued"] == 1
    finally:
        release.set();thread.join(5)
    assert not errors and not thread.is_alive()


def test_replay_does_not_send_again_or_include_new_recipients(authorized, monkeypatch):
    p = authorized
    calls = []
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: calls.append(args) or "wa-ok")
    ingest(p)
    p.deliveries.retry_due_once()
    p.db.session.add(p.base.Recipient(name="Ny", phone="+4544444444", active=True))
    p.db.session.commit()
    assert ingest(p).json["duplicate"] is True
    assert p.deliveries.retry_due_once()["sent"] == 0
    assert len(calls) == 1 and p.base.WhatsAppDelivery.query.count() == 1


def test_restart_recovers_pending_delivery_without_state(authorized, monkeypatch):
    p = authorized
    ingest(p)
    p.deliveries.WhatsAppRetryState.query.delete()
    p.db.session.commit()
    p.db.session.remove()
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: "recovered")
    assert p.deliveries.retry_due_once()["sent"] == 1


def test_duplicate_recovers_crash_before_enqueue(authorized, monkeypatch):
    p = authorized
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: "recovered")
    p.db.session.add(p.base.InboundMessage(source_id="alarm-1", sender="+4512345678", body="(S)M+V · Brand", received_at=p.base.utcnow(), accepted=True))
    p.db.session.commit()
    assert ingest(p).json["duplicate"] is True
    assert p.deliveries.retry_due_once()["sent"] == 1


@pytest.mark.parametrize("action", ["pause", "delete", "change_phone"])
def test_changed_recipient_is_not_sent_a_queued_alarm(authorized, monkeypatch, action):
    p = authorized
    calls = []
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: calls.append(args) or "wa-ok")
    ingest(p)
    recipient = p.base.Recipient.query.one()
    if action == "pause": recipient.active = False
    elif action == "delete": p.db.session.delete(recipient)
    else: recipient.phone = "+4555555555"
    p.db.session.commit()
    p.deliveries.retry_due_once()
    assert not calls and p.base.WhatsAppDelivery.query.one().status == "cancelled"


def test_expired_alarm_is_not_sent_when_connection_returns(authorized, monkeypatch):
    p = authorized
    calls = []
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: calls.append(args) or "wa-ok")
    ingest(p)
    state = p.deliveries.WhatsAppRetryState.query.one()
    state.first_failed_at = p.base.utcnow() - timedelta(hours=25)
    p.db.session.commit()
    p.deliveries.retry_due_once()
    assert not calls and p.base.WhatsAppDelivery.query.one().status == "failed"


def test_event_statistics_error_never_requeues_a_sent_alarm(authorized, monkeypatch):
    p = authorized
    calls = []
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: calls.append(args) or "wa-ok")
    monkeypatch.setattr(p.events, "mark_first_delivery", lambda *args: (_ for _ in ()).throw(RuntimeError("statistics")))
    ingest(p)
    assert p.deliveries.retry_due_once()["sent"] == 1
    p.deliveries.retry_due_once()
    assert len(calls) == 1 and p.base.WhatsAppDelivery.query.one().status == "sent"


def test_offline_retry_survives_database_session_restart(authorized, monkeypatch):
    p = authorized
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
    ingest(p)
    assert p.deliveries.retry_due_once()["sent"] == 0
    p.db.session.remove()
    assert p.base.WhatsAppDelivery.query.one().status == "retrying"
    p.deliveries.WhatsAppRetryState.query.one().next_attempt_at = p.base.utcnow()
    p.db.session.commit()
    monkeypatch.setattr(p.base, "send_whatsapp", lambda *args: "wa-back")
    assert p.deliveries.retry_due_once()["sent"] == 1


def test_test_alarm_is_opt_in_and_sending_two_uses_explicit_parent(authorized):
    p = authorized
    recipient = p.base.Recipient.query.one()
    p.db.session.add(p.stations.RecipientStationFilter(recipient_id=recipient.id, station="S"))
    p.db.session.commit()
    assert ingest(p, "test", "TEST alarm (S)").json["queued"] == 0
    ingest(p, "primary", eventKey="s-parent", messageKind="alarm_complete")
    ingest(p, "a-primary", "(A)M+V · Ny alarm", eventKey="a-parent", messageKind="alarm_complete")
    assert ingest(p, "followup", "Sending 2 · Ekstra melding", parentEventKey="s-parent", messageKind="sending2_complete").json["queued"] == 1


def test_deleting_event_cleans_retry_state_and_decision(authorized, client):
    p = authorized
    ingest(p)
    event_id = p.events.AlarmEvent.query.one().id
    assert client.post(f"/alarmer/{event_id}/slet", data={"csrf_token": "test-csrf"}).status_code == 302
    assert p.deliveries.WhatsAppRetryState.query.count() == 0
    assert p.deliveries.InboundDecision.query.count() == 0
    assert p.base.InboundMessage.query.count() == 0


def test_no_success_without_openwa_message_id(p, monkeypatch):
    monkeypatch.setattr(p.base, "openwa_request", lambda *args, **kwargs: {"error": "not ready"})
    monkeypatch.setenv("OPENWA_SESSION_ID", "test-session")
    with pytest.raises(RuntimeError, match="besked-id"):
        p.base.send_whatsapp("+4511111111", "Test")
