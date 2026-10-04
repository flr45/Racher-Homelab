from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def test_backup_channel_is_used_when_primary_is_not_ready(p, monkeypatch):
    monkeypatch.setenv("OPENWA_SESSION_ID", "primary-session")
    monkeypatch.setenv("BACKUP_OPENWA_BASE_URL", "http://openwa-backup:2785/api")
    monkeypatch.setenv("BACKUP_OPENWA_API_KEY", "backup-key")
    monkeypatch.setenv("BACKUP_OPENWA_SESSION_ID", "backup-session")

    monkeypatch.setattr(
        p.base,
        "_primary_openwa_status_uncached",
        lambda: {"state": "disconnected", "detail": "SBR-PAGER"},
    )
    monkeypatch.setattr(
        p.base,
        "_backup_openwa_status_uncached",
        lambda: {"state": "ready", "detail": "SBR-PAGER-BACKUP"},
    )

    primary_calls = []
    backup_calls = []
    monkeypatch.setattr(
        p.base,
        "openwa_request",
        lambda *args, **kwargs: primary_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        p.base,
        "backup_openwa_request",
        lambda *args, **kwargs: backup_calls.append((args, kwargs)) or {"messageId": "backup-ok"},
    )

    assert p.base.send_whatsapp("+4511111111", "Test") == "backup-ok"
    assert not primary_calls
    assert len(backup_calls) == 1
    assert "backup-session" in backup_calls[0][0][0]


def test_ready_primary_never_falls_through_to_backup_after_send_error(p, monkeypatch):
    monkeypatch.setenv("OPENWA_SESSION_ID", "primary-session")
    monkeypatch.setenv("BACKUP_OPENWA_BASE_URL", "http://openwa-backup:2785/api")
    monkeypatch.setenv("BACKUP_OPENWA_API_KEY", "backup-key")
    monkeypatch.setenv("BACKUP_OPENWA_SESSION_ID", "backup-session")

    monkeypatch.setattr(
        p.base,
        "_primary_openwa_status_uncached",
        lambda: {"state": "ready", "detail": "SBR-PAGER"},
    )
    backup_calls = []
    monkeypatch.setattr(
        p.base,
        "openwa_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous primary send failure")),
    )
    monkeypatch.setattr(
        p.base,
        "backup_openwa_request",
        lambda *args, **kwargs: backup_calls.append((args, kwargs)) or {"messageId": "must-not-send"},
    )

    with pytest.raises(RuntimeError, match="ambiguous primary"):
        p.base.send_whatsapp("+4511111111", "Test")
    assert not backup_calls


def _watchdog_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "sbr-pager-watchdog.py"
    spec = importlib.util.spec_from_file_location("sbr_pager_watchdog_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_watchdog_never_restarts_openwa_for_qr_ready(monkeypatch):
    watchdog = _watchdog_module()
    calls = []
    monkeypatch.setattr(
        watchdog,
        "recover_openwa_session",
        lambda: (_ for _ in ()).throw(AssertionError("must not start QR-ready session")),
    )
    monkeypatch.setattr(
        watchdog,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    action = watchdog.recover_component(
        "openwa",
        "OpenWA session status=qr_ready",
    )
    assert "QR-parring kræves" in action
    assert not calls


def test_watchdog_does_not_restart_when_session_is_already_started(monkeypatch):
    watchdog = _watchdog_module()
    calls = []
    monkeypatch.setattr(
        watchdog,
        "recover_openwa_session",
        lambda: (_ for _ in ()).throw(
            RuntimeError('OpenWA HTTP 400: {"message":"Session is already started"}')
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    action = watchdog.recover_component(
        "openwa",
        "OpenWA session status=disconnected",
    )
    assert "ingen containerrestart" in action
    assert not calls


def test_watchdog_understands_primary_failure_while_backup_is_active():
    watchdog = _watchdog_module()
    assert (
        watchdog.openwa_session_state_from_issue(
            "OpenWA primær status=qr_ready; backup aktiv"
        )
        == "qr_ready"
    )


def test_sms_fallback_is_used_when_both_whatsapp_channels_are_not_ready(p, monkeypatch):
    monkeypatch.setenv("OPENWA_SESSION_ID", "primary-session")
    monkeypatch.setenv("BACKUP_OPENWA_BASE_URL", "http://openwa-backup:2785/api")
    monkeypatch.setenv("BACKUP_OPENWA_API_KEY", "backup-key")
    monkeypatch.setenv("BACKUP_OPENWA_SESSION_ID", "backup-session")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_URL", "http://sms-backup:8080")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_TOKEN", "sms-backup-key")
    monkeypatch.setenv("SMS_FALLBACK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SMS_FALLBACK_POLL_SECONDS", "0.25")

    monkeypatch.setattr(
        p.base,
        "_primary_openwa_status_uncached",
        lambda: {"state": "disconnected", "detail": "SBR-PAGER"},
    )
    monkeypatch.setattr(
        p.base,
        "_backup_openwa_status_uncached",
        lambda: {"state": "qr_ready", "detail": "SBR-PAGER-BACKUP"},
    )

    calls = []
    responses = iter([
        {"id": 42, "status": "pending"},
        {"id": 42, "status": "sending"},
        {"id": 42, "status": "sent"},
    ])

    def fake_sms_request(url, token, **kwargs):
        calls.append((url, token, kwargs))
        return next(responses)

    monkeypatch.setattr(p.base, "_fallback_sms_request", fake_sms_request)

    assert p.base.send_whatsapp("+4511111111", "Alarm") == "sms-fallback:42"
    assert calls[0][0].endswith("/api/outgoing")
    assert calls[0][2]["method"] == "POST"
    assert calls[0][2]["payload"]["recipient"] == "+4511111111"
    assert calls[-1][0].endswith("/api/outgoing/42")


def test_sms_fallback_is_not_used_after_ambiguous_primary_send_failure(p, monkeypatch):
    monkeypatch.setenv("OPENWA_SESSION_ID", "primary-session")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_URL", "http://sms-backup:8080")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_TOKEN", "sms-backup-key")
    monkeypatch.setattr(
        p.base,
        "_primary_openwa_status_uncached",
        lambda: {"state": "ready", "detail": "SBR-PAGER"},
    )
    monkeypatch.setattr(
        p.base,
        "openwa_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous primary send failure")),
    )
    sms_calls = []
    monkeypatch.setattr(
        p.base,
        "send_fallback_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or "must-not-send",
    )

    with pytest.raises(RuntimeError, match="ambiguous primary"):
        p.base.send_whatsapp("+4511111111", "Alarm")
    assert not sms_calls


def test_sms_fallback_failure_stays_in_durable_retry_queue(authorized, monkeypatch):
    p = authorized
    monkeypatch.setenv("OPENWA_SESSION_ID", "primary-session")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_URL", "http://sms-backup:8080")
    monkeypatch.setenv("SMS_FALLBACK_GATEWAY_TOKEN", "sms-backup-key")
    monkeypatch.setattr(
        p.base,
        "_primary_openwa_status_uncached",
        lambda: {"state": "disconnected", "detail": "SBR-PAGER"},
    )
    monkeypatch.setattr(
        p.base,
        "send_fallback_sms",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("SMS modem offline")),
    )

    from test_delivery import ingest

    assert ingest(p, source="sms-fallback-test").status_code == 201
    assert p.deliveries.retry_due_once()["sent"] == 0
    delivery = p.base.WhatsAppDelivery.query.one()
    assert delivery.status == "retrying"
    assert "SMS modem offline" in (delivery.error or "")
