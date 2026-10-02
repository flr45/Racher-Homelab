import pytest
from bs4 import BeautifulSoup

HEADERS = {"Authorization": "Bearer test-ingest"}


def incoming(p, source="new-number", body="(A) Almindelig melding fra ukendt nummer"):
    return p.app.test_client().post("/api/incoming", json={"sender": "+4533333333", "body": body,
        "sourceMessageId": source, "receivedAt": "2026-09-30T21:00:00Z"}, headers=HEADERS)


def enable(client):
    return client.post("/indstillinger/afsenderfilter", data={"csrf_token": "test-csrf", "accept_all": "1"})


def test_unknown_numbers_are_blocked_by_default(authorized):
    response = incoming(authorized)
    assert response.status_code == 202 and response.json["accepted"] is False
    assert authorized.base.WhatsAppDelivery.query.count() == 0


def test_checkbox_persists_and_queues_unknown_numbers(authorized, client):
    p = authorized
    assert enable(client).status_code == 302
    p.db.session.remove()
    assert p.stations.accept_all_sms_senders() is True
    response = incoming(p)
    assert response.status_code == 201 and response.json["queued"] == 1
    html = BeautifulSoup(client.get("/indstillinger").text, "html.parser")
    assert html.select_one('#accept-all').has_attr('checked')


def test_turning_off_restores_allowlist_without_erasing_approved_numbers(authorized, client):
    p = authorized
    enable(client)
    client.post("/indstillinger/afsenderfilter", data={"csrf_token": "test-csrf"})
    assert p.stations.accept_all_sms_senders() is False
    assert p.base.AllowedSender.query.count() == 1
    assert incoming(p).json["accepted"] is False


def test_enabling_does_not_replay_previously_rejected_sms(authorized, client):
    p = authorized
    assert incoming(p).json["accepted"] is False
    enable(client)
    assert incoming(p).json["duplicate"] is True
    assert p.base.WhatsAppDelivery.query.count() == 0
    assert incoming(p, source="next-message").json["queued"] == 1


@pytest.mark.parametrize("body", ["(S) Melding til anden station", "(A) Test af alarm", "\x00@@@@@@@@@@@@"])
def test_all_numbers_keeps_station_test_and_noise_filters(authorized, client, body):
    p = authorized
    recipient = p.base.Recipient.query.one()
    p.db.session.add(p.stations.RecipientStationFilter(recipient_id=recipient.id, station="A"))
    p.db.session.commit()
    enable(client)
    incoming(p, body=body)
    assert p.base.WhatsAppDelivery.query.count() == 0


def test_filter_requires_admin_csrf_and_ingest_token(authorized, client):
    p = authorized
    assert p.app.test_client().post('/indstillinger/afsenderfilter', data={'accept_all':'1'}).status_code == 302
    assert client.post('/indstillinger/afsenderfilter', data={'accept_all':'1'}).status_code == 400
    enable(client)
    assert p.app.test_client().post('/api/incoming', json={'sender':'+4533333333','body':'Melding'}).status_code == 401
