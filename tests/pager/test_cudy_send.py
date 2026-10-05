from cudy_client import CudyClient, CudyError, FormPage
import cudy_reader
from cudy_reader import outgoing_sms_parts


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


def test_cudy_send_enables_before_submit_and_leaves_engine_on():
    client = FakeCudy()

    result = client.send_sms(
        "+4522270396",
        "Alarmtest",
        confirm_seconds=1,
        poll_seconds=0.01,
        settle_seconds=0,
    )

    assert result["accepted"] is True
    assert result["cfg"] == "cfg-new"
    assert client.enabled is True
    assert result["sms_engine_enabled"] is True
    assert client.events == [
        ("enabled", True),
        ("prepare", "+4522270396", "Alarmtest", True),
        ("submit", True),
    ]


def test_cudy_send_reconciles_outbox_after_ambiguous_submit_error():
    client = FakeCudy(submit_error=True)

    result = client.send_sms(
        "+4522270396",
        "Alarmtest",
        confirm_seconds=1,
        poll_seconds=0.01,
        settle_seconds=0,
    )

    assert result["accepted"] is True
    assert result["cfg"] == "cfg-new"
    assert client.enabled is True


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

    assert client.enabled is True


def test_sms_engine_self_heal_is_idempotent():
    client = FakeCudy()

    assert client.ensure_sms_enabled() is True
    assert client.enabled is True
    assert client.events == [("enabled", True)]

    assert client.ensure_sms_enabled() is True
    assert client.events == [("enabled", True)]


def test_cudy_outgoing_api_is_enabled_by_default_and_has_kill_switch(g, monkeypatch):
    monkeypatch.setenv("SMS_MODEM_DRIVER", "cudy")
    monkeypatch.delenv("CUDY_SMS_SEND_ENABLED", raising=False)
    client = g.app.test_client()
    headers = {"Authorization": "Bearer test-gateway"}

    queued = client.post(
        "/api/outgoing",
        json={"recipient": "+4522270396", "body": "Alarmtest"},
        headers=headers,
    )
    assert queued.status_code == 202

    monkeypatch.setenv("CUDY_SMS_SEND_ENABLED", "false")
    blocked = client.post(
        "/api/outgoing",
        json={"recipient": "+4522270396", "body": "Alarmtest 2"},
        headers=headers,
    )
    assert blocked.status_code == 409

    monkeypatch.setenv("CUDY_SMS_SEND_ENABLED", "true")

    claimed = client.post(
        "/api/outgoing/claim",
        json={},
        headers=headers,
    )
    assert claimed.status_code == 200
    assert claimed.get_json()["recipient"] == "+4522270396"
    assert claimed.get_json()["created_at"]

def test_outbox_ids_match_observed_lt300_more_details_markup():
    document = """
    <table><tr><td>
      <button onclick='cbi_show_modal(this,
        "/cgi-bin/luci/admin/network/gcom/sms/readsms",
        "iface=4g&cfg=cfg0bab7b&smsbox=sto");return false;'>
        More Details
      </button>
    </td></tr></table>
    """

    class HtmlClient(CudyClient):
        def __init__(self):
            self.root = "http://cudy.test/cgi-bin/luci/"

        def _authenticated_page(self, url):
            return FormPage(document)

    assert HtmlClient().outbox_ids() == ["cfg0bab7b"]


def test_outbox_message_reads_phone_and_textarea():
    document = """
    <form action="/cgi-bin/luci/admin/network/gcom/sms/readsms">
      <input name="token" value="secret">
      <input name="cbid.smsread.1.phone" value="+4522270396">
      <textarea name="cbid.smsread.1.content">Alarmtest</textarea>
    </form>
    """

    class DetailClient(CudyClient):
        def __init__(self):
            self.root = "http://cudy.test/cgi-bin/luci/"

        def _authenticated_page(self, url):
            return FormPage(document)

    item = DetailClient().outbox_message("cfg0bab7b")
    assert item == {
        "cfg": "cfg0bab7b",
        "recipient": "+4522270396",
        "body": "Alarmtest",
    }

def test_short_outgoing_sms_gets_stable_job_marker():
    message = {
        "id": 42,
        "created_at": "2026-10-04T16:10:00+00:00",
        "recipient": "+4522270396",
        "body": "Kort alarm",
    }

    first = outgoing_sms_parts(message)
    second = outgoing_sms_parts(message)

    assert first == second
    assert len(first) == 1
    assert first[0].startswith("[SBR ")
    assert first[0].endswith("Kort alarm")
    assert len(first[0]) <= cudy_reader.MAX_SMS_CHARS


def test_long_outgoing_sms_is_split_into_cudy_safe_parts():
    body = " ".join(["Alarmtekst"] * 80)
    message = {
        "id": 43,
        "created_at": "2026-10-04T16:11:00+00:00",
        "recipient": "+4522270396",
        "body": body,
    }

    parts = outgoing_sms_parts(message)

    assert len(parts) > 1
    assert all(len(part) <= cudy_reader.MAX_SMS_CHARS for part in parts)
    assert all(f"{index}/{len(parts)}" in part for index, part in enumerate(parts, 1))

def test_cudy_reader_keeps_sms_enabled_for_whole_multipart_batch(monkeypatch):
    message = {
        "id": 77,
        "created_at": "2026-10-04T16:20:00+00:00",
        "recipient": "+4522270396",
        "body": " ".join(["Lang alarmtekst"] * 40),
    }
    expected_parts = outgoing_sms_parts(message)
    assert len(expected_parts) > 1

    jobs = [message, None]
    completed = []

    monkeypatch.setattr(cudy_reader, "SEND_ENABLED", True)
    monkeypatch.setattr(cudy_reader.reader, "SMS_DRY_RUN", False)
    monkeypatch.setattr(cudy_reader.reader, "OUTBOX_BATCH_SIZE", 20)
    monkeypatch.setattr(cudy_reader.reader, "running", True)
    monkeypatch.setattr(cudy_reader.reader, "claim_outgoing", lambda: jobs.pop(0))
    monkeypatch.setattr(
        cudy_reader.reader,
        "complete_outgoing",
        lambda message_id, status, error=None, retry=False: completed.append(
            (message_id, status, error, retry)
        ),
    )
    monkeypatch.setattr(cudy_reader.reader, "write_status", lambda **values: None)
    monkeypatch.setattr(
        cudy_reader.reader,
        "utc_iso",
        lambda: "2026-10-04T16:20:10+00:00",
    )

    class BatchClient:
        def __init__(self):
            self.enabled = False
            self.enable_events = []
            self.sent = []

        def find_outbox_message(self, recipient, part):
            return None

        def sms_enabled(self):
            return self.enabled

        def set_sms_enabled(self, enabled):
            self.enabled = bool(enabled)
            self.enable_events.append(self.enabled)
            return self.enabled

        def ensure_sms_enabled(self):
            if not self.enabled:
                self.set_sms_enabled(True)
            return True

        def send_sms(self, recipient, part):
            assert self.enabled is True
            self.sent.append((recipient, part))
            return {
                "accepted": True,
                "cfg": f"cfg{len(self.sent)}",
                "sms_engine_enabled": True,
            }

    client = BatchClient()
    cudy_reader.process_outbox(client)

    assert client.enable_events == [True]
    assert [part for _, part in client.sent] == expected_parts
    assert completed == [(77, "sent", None, False)]
