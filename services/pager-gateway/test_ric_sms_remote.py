from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import ric_sms
from ric_sms_remote import AuthenticatedRicSmsRouter
from storage import Storage


class _Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"id": 91, "status": "pending"}).encode("utf-8")


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **_unused):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class _Event:
    def __init__(self, payload):
        self.payload = dict(payload)
        for key, value in self.payload.items():
            setattr(self, key, value)

    def to_dict(self):
        return dict(self.payload)


class RicSmsRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        storage = Storage(self.db)

        def ingest(event):
            return storage.add_message(event.to_dict())

        self.core = SimpleNamespace(
            DB_PATH=self.db,
            storage=storage,
            ingest_event=ingest,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *args, **kwargs: None)),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_remote_transport_sends_bearer_token(self):
        router = AuthenticatedRicSmsRouter(self.core)
        captured = []

        def fake_urlopen(request, timeout=0):
            captured.append(request)
            return _Response()

        with patch.dict(os.environ, {"PAGER_SMS_GATEWAY_TOKEN": "bridge-secret"}, clear=False):
            with patch("ric_sms_remote.urllib.request.urlopen", side_effect=fake_urlopen):
                result = router._post_outgoing(
                    "http://100.64.0.10:8090", "+4512345678", "Alarm"
                )

        self.assertEqual(result["id"], 91)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer bridge-secret")
        self.assertEqual(captured[0].full_url, "http://100.64.0.10:8090/api/outgoing")

    def test_late_transitive_duplicate_ric_triggers_one_sms_for_root_alarm(self):
        router = AuthenticatedRicSmsRouter(self.core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        router.store.add_rule("0001133", "+4512345678", "Slagelse")

        sent = []
        router._post_outgoing = lambda gateway_url, recipient, body: sent.append(
            (gateway_url, recipient, body)
        ) or {"id": 77, "status": "pending"}

        base = {
            "received_at": "2026-08-21T16:00:02",
            "protocol": "POCSAG",
            "baud": 2400,
            "function": "1",
            "station": "Slagelse",
            "message": "DAGENS PRØVE TIL SLAGELSE",
            "raw_line": "raw",
            "source": "pdl-file",
            "relevance_class": "relevant",
        }

        original_thread = ric_sms.threading.Thread
        ric_sms.threading.Thread = _ImmediateThread
        try:
            root = self.core.ingest_event(_Event({
                **base,
                "ric": "0001125",
                "delivery_eligible": True,
            }))
            middle = self.core.ingest_event(_Event({
                **base,
                "ric": "0001143",
                "delivery_eligible": False,
                "suppressed_reason": "duplicate",
                "duplicate_of": root,
            }))
            target = self.core.ingest_event(_Event({
                **base,
                "ric": "0001133",
                "delivery_eligible": False,
                "suppressed_reason": "duplicate",
                "duplicate_of": middle,
            }))
            # A further duplicate carrying the same target RIC must not send again.
            self.core.ingest_event(_Event({
                **base,
                "ric": "0001133",
                "delivery_eligible": False,
                "suppressed_reason": "duplicate",
                "duplicate_of": target,
            }))
        finally:
            ric_sms.threading.Thread = original_thread

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "+4512345678")
        deliveries = router.store.list_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["message_id"], root)
        self.assertEqual(deliveries[0]["status"], "queued")
        self.assertEqual(deliveries[0]["matched_rics"], "0001133")
        self.assertIn("0001133", router._event_rics(root, {"ric": "0001125"}))


if __name__ == "__main__":
    unittest.main()
