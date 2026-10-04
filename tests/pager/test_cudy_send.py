from cudy_client import CudyClient, CudyError


class FakeCudy(CudyClient):
    def __init__(self, *, submit_error=False, expose_outbox=True):
        self.enabled = False
        self.submit_error = submit_error
        self.expose_outbox = expose_outbox
        self.submitted = False
        self.events = []

    def sms_enabled(self):
        return self.enabled

    def set_sms_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.events.append(("enabled", self.enabled))
        return self.enabled

    def outbox_ids(self):
        if self.submitted and self.expose_outbox:
            return ["cfg-new"]
        return []

    def outbox_message(self, cfg):
        assert cfg == "cfg-new"
        return {
            "cfg": cfg,
            "recipient": "+4522270396",
            "body": "Alarmtest",
        }

    def prepare_sms(self, recipient, body):
        self.events.append(("prepare", recipient, body, self.enabled))
        return "http://cudy.test/smsnew", {"body": body}

    def _request(self, url, fields=None):
        self.events.append(("submit", self.enabled))
        self.submitted = True
        if self.submit_error:
            raise CudyError("simuleret transportfejl")
        return object()


def test_cudy_send_enables_before_submit_and_restores_afterwards():
    client = FakeCudy()

    result = client.send_sms(
        "+4522270396",
        "Alarmtest",
        confirm_seconds=1,
        poll_seconds=0.01,
    )

    assert result["accepted"] is True
    assert result["cfg"] == "cfg-new"
    assert client.enabled is False
    assert client.events == [
        ("enabled", True),
        ("prepare", "+4522270396", "Alarmtest", True),
        ("submit", True),
        ("enabled", False),
    ]


def test_cudy_send_reconciles_outbox_after_ambiguous_submit_error():
    client = FakeCudy(submit_error=True)

    result = client.send_sms(
        "+4522270396",
        "Alarmtest",
        confirm_seconds=1,
        poll_seconds=0.01,
    )

    assert result["accepted"] is True
    assert result["cfg"] == "cfg-new"
    assert client.enabled is False


def test_cudy_send_fails_closed_without_new_outbox_entry():
    client = FakeCudy(expose_outbox=False)

    try:
        client.send_sms(
            "+4522270396",
            "Alarmtest",
            confirm_seconds=0.01,
            poll_seconds=0.01,
        )
    except CudyError as exc:
        assert "Outbox-post" in str(exc)
    else:
        raise AssertionError("Cudy send skulle have fejlet uden Outbox-kvittering")

    assert client.enabled is False


def test_cudy_outgoing_api_requires_explicit_send_flag(g, monkeypatch):
    monkeypatch.setenv("SMS_MODEM_DRIVER", "cudy")
    monkeypatch.delenv("CUDY_SMS_SEND_ENABLED", raising=False)
    client = g.app.test_client()
    headers = {"Authorization": "Bearer test-gateway"}

    blocked = client.post(
        "/api/outgoing",
        json={"recipient": "+4522270396", "body": "Alarmtest"},
        headers=headers,
    )
    assert blocked.status_code == 409

    monkeypatch.setenv("CUDY_SMS_SEND_ENABLED", "true")
    queued = client.post(
        "/api/outgoing",
        json={"recipient": "+4522270396", "body": "Alarmtest"},
        headers=headers,
    )
    assert queued.status_code == 202

    claimed = client.post(
        "/api/outgoing/claim",
        json={},
        headers=headers,
    )
    assert claimed.status_code == 200
    assert claimed.get_json()["recipient"] == "+4522270396"
