import json
from datetime import timedelta
import pytest

HEADERS = {"Authorization": "Bearer test-gateway"}


@pytest.mark.parametrize("path,payload", [
    ("/api/incoming", []),
    ("/api/incoming", {"sender": "+4512345678", "body": {"invalid": True}, "sourceMessageId": "bad-input", "receivedAt": "2026-09-30T20:00:00Z"}),
    ("/api/outgoing", []),
    ("/api/outgoing", {"recipient": "+4512345678", "body": ["invalid"]}),
])
def test_invalid_gateway_payloads_are_rejected_without_persistence(g, path, payload):
    assert g.app.test_client().post(path, json=payload, headers=HEADERS).status_code == 400
    assert g.base.InboundMessage.query.count() == 0
    assert g.OutboundMessage.query.count() == 0


def test_repeated_modem_import_creates_one_status_command(g):
    c = g.app.test_client()
    payload = {"sender": "+4512345678", "body": "status", "receivedAt": "2026-09-30T20:00:00Z", "sourceMessageId": "usb-test-1"}
    assert c.post("/api/incoming", json=payload, headers=HEADERS).status_code == 201
    assert c.post("/api/incoming", json=payload, headers=HEADERS).status_code == 201
    assert g.CommandRequest.query.count() == 1
    assert g.base.InboundMessage.query.count() == 1


def test_crash_before_source_receipt_does_not_repeat_command(g):
    g.process_incoming("+4512345678", "status", "2026-09-30T20:00:00Z", "source-1")
    g.SourceReceipt.query.delete(); g.db.session.commit()
    g.process_incoming("+4512345678", "status", "2026-09-30T20:00:00Z", "source-1")
    assert g.CommandRequest.query.count() == 1


def test_cudy_keeps_usb_outgoing_jobs_and_rejects_unsupported_sending(g, monkeypatch):
    c = g.app.test_client()
    queued = c.post("/api/outgoing", json={"recipient": "+4511111111", "body": "test"}, headers=HEADERS)
    assert queued.status_code == 202
    monkeypatch.setenv("SMS_MODEM_DRIVER", "cudy")
    assert c.post("/api/outgoing", json={"recipient": "+4511111111", "body": "test"}, headers=HEADERS).status_code == 409
    assert c.post("/api/outgoing/claim", json={}, headers=HEADERS).status_code == 204
    assert g.OutboundMessage.query.one().status == "pending"


def test_stale_reader_never_reports_online(g):
    g.base.MODEM_STATUS_FILE.write_text(json.dumps({"state": "online", "updated_at": (g.utcnow()-timedelta(minutes=5)).isoformat()}))
    result = g.app.test_client().get("/health").json
    assert result["modem"]["state"] == "stale" and result["status"] == "degraded"


def test_corrupt_pdu_does_not_block_valid_alarm():
    import sms_pdu
    pdu = "06915404969912040A91542272306900006280107085538031A8600A442DCFE920F80364EEC8C9E9331D8466CCE9653AE8DD963FC86550BB4C0675400CD003C4012D400E"
    response = "+CMGL: 1,0,,2\n00\n+CMGL: 2,0,,40\n" + pdu + "\nOK\n"
    parts = sms_pdu.parse_cmgl_parts(response)
    assert len(parts) == 1 and parts[0].index == 2 and "færdigt" in parts[0].text


def test_missing_pdu_does_not_skip_next_header():
    import sms_pdu
    pdu = "06915404969912040A91542272306900006280107085538031A8600A442DCFE920F80364EEC8C9E9331D8466CCE9653AE8DD963FC86550BB4C0675400CD003C4012D400E"
    parts = sms_pdu.parse_cmgl_parts("+CMGL: 1,0,,2\n+CMGL: 2,0,,40\n" + pdu + "\nOK\n")
    assert [part.index for part in parts] == [2]
