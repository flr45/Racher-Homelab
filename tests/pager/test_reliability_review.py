"""Fault injection for reconnects, ambiguous sends and host monitoring.

No test talks to a modem, WhatsApp, Docker or a notification recipient.
"""
import importlib.util
import io
import sys
import urllib.error
from pathlib import Path

import pytest


def watchdog():
    spec = importlib.util.spec_from_file_location(
        "watchdog_reliability", Path(__file__).parents[2] / "scripts/sbr-pager-watchdog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def incoming(p):
    response = p.app.test_client().post("/api/incoming", json={
        "sender": "+4512345678", "body": "(S)M+V · Brand · Eksempelvej 12",
        "sourceMessageId": "reliability-alarm", "receivedAt": p.base.utcnow().isoformat(),
    }, headers={"Authorization": "Bearer test-ingest"})
    assert response.status_code == 201


def test_liveness_does_not_contact_whatsapp(p, monkeypatch):
    def forbidden():
        raise AssertionError("liveness must never depend on the WhatsApp network")
    monkeypatch.setattr(p.base, "openwa_status", forbidden)
    response = p.app.test_client().get("/health/live")
    assert response.status_code == 200
    assert response.json == {"status": "ok", "database": "online"}


@pytest.mark.parametrize("failure", [TimeoutError("lost response"), RuntimeError("OpenWA HTTP 500: engine failure")])
def test_ambiguous_alarm_is_held_and_never_automatically_sent_again(authorized, monkeypatch, failure):
    p = authorized
    monkeypatch.setenv("OPENWA_SESSION_ID", "test-session")
    sends = []
    def accepted_then_failed(*args, **kwargs):
        sends.append(args)
        raise failure
    monkeypatch.setattr(p.base, "openwa_request", accepted_then_failed)
    incoming(p)
    p.deliveries.retry_due_once()
    p.db.session.remove()  # Demonstrate that the hold survives a session restart.
    row = p.base.WhatsAppDelivery.query.one()
    assert row.status == "held" and "Uafklaret" in row.error
    assert p.deliveries.WhatsAppRetryState.query.one().next_attempt_at is None
    p.deliveries.retry_due_once()
    assert len(sends) == 1
    assert p.deliveries.queue_snapshot()["held"] == 1


def test_refused_connection_can_retry_without_holding_the_alarm(authorized, monkeypatch):
    p = authorized
    monkeypatch.setenv("OPENWA_SESSION_ID", "test-session")
    def refused(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError("API not listening"))
    monkeypatch.setattr(p.base, "openwa_request", refused)
    incoming(p)
    p.deliveries.retry_due_once()
    assert p.base.WhatsAppDelivery.query.one().status == "retrying"
    state = p.deliveries.WhatsAppRetryState.query.one()
    state.next_attempt_at = p.base.utcnow()
    p.db.session.commit()
    monkeypatch.setattr(p.base, "openwa_request", lambda *args, **kwargs: {"messageId": "recovered"})
    assert p.deliveries.retry_due_once()["sent"] == 1


def test_send_key_survives_retry_and_changes_only_after_explicit_review(authorized, client, monkeypatch):
    p = authorized
    monkeypatch.setenv("OPENWA_API_KEY", "test-key")
    monkeypatch.setenv("OPENWA_SESSION_ID", "test-session")
    keys = []
    def request(req, timeout):
        keys.append(req.get_header("Idempotency-key"))
        raise urllib.error.URLError(ConnectionRefusedError("API not listening"))
    monkeypatch.setattr(p.base.urllib.request, "urlopen", request)
    incoming(p)
    p.deliveries.retry_due_once()
    state = p.deliveries.WhatsAppRetryState.query.one()
    state.next_attempt_at = p.base.utcnow()
    p.db.session.commit()
    p.deliveries.retry_due_once()
    assert keys[0] and keys[0] == keys[1]
    row = p.base.WhatsAppDelivery.query.one()
    row.status = "held"
    p.db.session.commit()
    response = client.post(f"/drift/ko/{row.inbound_id}", data={"csrf_token": "test-csrf", "confirm": "1", "action": "release"})
    assert response.status_code == 302
    p.deliveries.retry_due_once()
    assert keys[-1] != keys[0]


def test_sms_fallback_keeps_accepted_queue_id_on_status_timeout(p, monkeypatch):
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_URL", "http://sms:8080")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_TOKEN", "test-sms-key")
    calls = []
    def request(url, token, **kwargs):
        calls.append(kwargs.get("method", "GET"))
        if kwargs.get("method") == "POST":
            return {"id": 42}
        raise TimeoutError("status lost")
    monkeypatch.setattr(p.base, "_fallback_sms_request", request)
    with pytest.raises(p.base.DeliveryOutcomeUnknown) as result:
        p.base.send_fallback_sms("+4512345678", "Alarm")
    assert result.value.message_id == "sms-fallback:42"
    assert calls == ["POST", "GET"]


@pytest.mark.parametrize("state", ["ready", "qr_ready", "authenticated", "auth_failure"])
def test_recovery_rechecks_session_before_mutating(monkeypatch, state):
    w = watchdog()
    monkeypatch.setenv("SMS_WHATSAPP_OPENWA_SESSION_ID", "session-1")
    monkeypatch.setattr(w, "docker_state", lambda name: ("running", "healthy"))
    calls = []
    def api(path, method="GET", timeout=75):
        calls.append((path, method))
        assert method == "GET"
        return {"status": state, "engineLoaded": True}
    monkeypatch.setattr(w, "openwa_api_request", api)
    w.recover_openwa_session()
    assert calls == [("sessions/session-1", "GET")]


def test_stalled_loaded_session_is_stopped_then_started_without_logout(monkeypatch):
    w = watchdog()
    monkeypatch.setenv("SMS_WHATSAPP_OPENWA_SESSION_ID", "session-1")
    monkeypatch.setattr(w, "docker_state", lambda name: ("running", "healthy"))
    calls = []
    def api(path, method="GET", timeout=75):
        calls.append((path, method))
        return {"status": "disconnected", "engineLoaded": True}
    monkeypatch.setattr(w, "openwa_api_request", api)
    w.recover_openwa_session()
    assert calls == [("sessions/session-1", "GET"), ("sessions/session-1/stop", "POST"), ("sessions/session-1/start", "POST")]


def test_already_started_on_older_image_recovers_the_session_not_container(monkeypatch):
    w = watchdog()
    monkeypatch.setenv("SMS_WHATSAPP_OPENWA_SESSION_ID", "session-1")
    monkeypatch.setattr(w, "docker_state", lambda name: ("running", "healthy"))
    calls = []
    def api(path, method="GET", timeout=75):
        calls.append((path, method))
        if path.endswith("/start") and len(calls) == 2:
            raise RuntimeError("Session is already started")
        return {"status": "disconnected"}
    monkeypatch.setattr(w, "openwa_api_request", api)
    w.recover_openwa_session()
    assert calls[-2:] == [("sessions/session-1/stop", "POST"), ("sessions/session-1/start", "POST")]
    assert all("logout" not in path and "delete" not in path for path, _ in calls)


def test_recent_initializing_session_is_not_stopped(monkeypatch):
    w = watchdog()
    monkeypatch.setenv("SMS_WHATSAPP_OPENWA_SESSION_ID", "session-1")
    monkeypatch.setattr(w, "docker_state", lambda name: ("running", "healthy"))
    def api(path, method="GET", timeout=75):
        assert method == "GET"
        return {"status": "initializing", "engineLoaded": True, "updatedAt": w.datetime.now(w.timezone.utc).isoformat()}
    monkeypatch.setattr(w, "openwa_api_request", api)
    assert "opstartsro" in w.recover_openwa_session()


def prepare_watchdog_main(monkeypatch, w, state):
    monkeypatch.setattr(sys, "argv", ["sbr-watchdog"])
    monkeypatch.setattr(w, "load_env", lambda path: None)
    monkeypatch.setattr(w, "load_state", lambda: state)
    monkeypatch.setattr(w, "save_state", lambda value: None)
    monkeypatch.setenv("SBR_WATCHDOG_FAIL_THRESHOLD", "3")
    monkeypatch.setattr(w, "run", lambda *args, **kwargs: pytest.fail("must not restart a container for a queue warning"))
    monkeypatch.setattr(w, "compose_up", lambda *args, **kwargs: pytest.fail("must not recreate Pager for a queue warning"))


def test_growing_queue_age_reaches_threshold_without_container_restart(monkeypatch):
    w, state = watchdog(), {}
    prepare_watchdog_main(monkeypatch, w, state)
    notices = []
    monkeypatch.setattr(w, "notify", lambda *args, **kwargs: notices.append(args) or True)
    for minute in (15, 16, 17):
        monkeypatch.setattr(w.time, "time", lambda minute=minute: 100000 + minute * 60)
        monkeypatch.setattr(w, "collect_checks", lambda minute=minute: ({"gateway": None, "pager": f"WhatsApp retry-køens ældste besked er {minute} min", "openwa": None}, {}))
        w.main()
    assert state["components"]["pager"]["failures"] == 3
    assert len(notices) == 1
    assert "ingen containerrestart" in state["components"]["pager"]["last_recovery_action"]


def test_failed_notice_is_retried_and_never_marked_delivered(monkeypatch):
    w, state = watchdog(), {}
    prepare_watchdog_main(monkeypatch, w, state)
    monkeypatch.setenv("SBR_WATCHDOG_FAIL_THRESHOLD", "1")
    monkeypatch.setattr(w, "collect_checks", lambda: ({"gateway": None, "pager": "WhatsApp retry-kø har 1 permanent fejlede leveringer", "openwa": None}, {}))
    notices = []
    monkeypatch.setattr(w, "notify", lambda *args, **kwargs: notices.append(args) or False)
    for stamp in (100000, 100060, 100301):
        monkeypatch.setattr(w.time, "time", lambda stamp=stamp: stamp)
        w.main()
    assert len(notices) == 2
    assert not state["components"]["pager"].get("alerted", False)
    assert not state["components"]["pager"].get("last_notified")


def test_both_channels_need_qr_reports_actual_primary_state(monkeypatch):
    w = watchdog()
    monkeypatch.setattr(w, "container_issue", lambda component: None)
    monkeypatch.setattr(w, "container_age_seconds", lambda name: 10000)
    gateway = {"status": "ok", "gateway": {"database": "online"}, "modem": {"state": "online"}}
    pager = {"status": "ok", "database": "online", "deliveryQueue": {}, "openwa": {"state": "offline", "active": None, "primary": {"state": "qr_ready"}, "backup": {"state": "qr_ready"}}}
    monkeypatch.setattr(w, "http_json", lambda url, timeout=8: gateway if ":8090/" in url else pager)
    checks, _ = w.collect_checks()
    assert checks["openwa"] == "OpenWA session status=qr_ready"


def test_cudy_request_has_the_headers_required_by_live_firmware():
    from cudy_client import CudyClient
    class Router:
        def open(self, req, timeout):
            assert req.get_header("Origin") == "http://192.168.10.1"
            assert req.get_header("Referer") == req.full_url
            assert req.get_header("X-requested-with") == "XMLHttpRequest"
            return io.BytesIO(b"<html></html>")
    CudyClient(opener=Router())._request("http://192.168.10.1/cgi-bin/luci/", {"token": "synthetic"})


def test_cudy_small_part_limit_fails_the_job_without_stopping_reader(monkeypatch):
    import cudy_reader as c
    monkeypatch.setattr(c, "MAX_SMS_CHARS", 10)
    monkeypatch.setattr(c, "SEND_ENABLED", True)
    monkeypatch.setattr(c.reader, "running", True)
    job = {"id": 1, "recipient": "+4512345678", "body": "Long alarm text"}
    monkeypatch.setattr(c.reader, "claim_outgoing", lambda: job)
    completed = []
    monkeypatch.setattr(c.reader, "complete_outgoing", lambda *args, **kwargs: completed.append((args, kwargs)))
    monkeypatch.setattr(c.reader, "write_status", lambda **kwargs: None)
    c.process_outbox(object())
    assert completed[0][0] == (1, "failed")
    assert completed[0][1]["retry"] is False


def test_unicode_ingest_token_is_validated_without_server_error(p, monkeypatch):
    monkeypatch.setenv("SMS_WHATSAPP_INGEST_TOKEN", "nøgle-æ")
    client = p.app.test_client()
    assert client.post("/api/incoming", json={}, headers={"Authorization": "Bearer forkert-ø"}).status_code == 401
    assert client.post("/api/incoming", json={}, headers={"Authorization": "Bearer nøgle-æ"}).status_code == 400


def test_cudy_physical_send_result_can_be_recorded_with_active_support(p, client, monkeypatch):
    monkeypatch.setattr(p, "gateway_status", lambda: {"modem": {
        "transport": "cudy", "state": "online", "capability": "send-receive"}})
    data = {"csrf_token": "test-csrf", "result": "passed", "confirm": "1", "notes": "Prøve modtaget på telefon"}
    assert client.post("/indkoering/cudy/smsout", data=data).status_code == 302
    from observability import CommissioningStep
    assert CommissioningStep.query.filter_by(driver="cudy", step="smsout").one().result == "passed"


@pytest.mark.parametrize("wa,expected", [
    ({"state": "disconnected", "detail": "Session"}, "afventer genopretning"),
    ({"state": "qr_ready", "detail": "Session"}, "kræver ny parring"),
    ({"state": "offline", "primary": {"state": "qr_ready"}, "detail": "Begge kanaler offline"}, "kræver ny parring"),
])
def test_connection_page_distinguishes_disconnect_from_qr(p, client, monkeypatch, wa, expected):
    monkeypatch.setattr(p.base, "openwa_status", lambda: wa)
    response = client.get("/forbindelser")
    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)
