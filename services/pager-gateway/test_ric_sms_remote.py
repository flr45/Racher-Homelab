from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


class RicSmsRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        storage = Storage(self.db)
        self.core = SimpleNamespace(
            DB_PATH=self.db,
            storage=storage,
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


if __name__ == "__main__":
    unittest.main()
