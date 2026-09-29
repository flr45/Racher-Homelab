from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import whatsapp_extension
from routing import RoutingStore
from storage import Storage
from whatsapp_extension import WhatsAppDelivery


class _Logger:
    def warning(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **_unused):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class WhatsAppDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)
        self.routing = RoutingStore(self.db)
        self.user_id = self.storage.create_user(
            "frederik", "Frederik", "hash", "admin", None
        )
        self.routing.set_user_receive_all(self.user_id, True)
        self.delivery = WhatsAppDelivery(
            SimpleNamespace(logger=_Logger()),
            self.storage,
            self.routing,
        )
        self.delivery.set_preference(
            self.user_id, enabled=True, phone="+4512345678"
        )
        self.message_id = self.storage.add_message({
            "received_at": "2026-09-29T10:00:00+00:00",
            "protocol": "POCSAG",
            "baud": 1200,
            "ric": "0006240",
            "station": "Ringsted",
            "message": "BRANDALARM Testvej 1",
            "raw_line": "raw",
            "source": "pdl-file",
            "delivery_eligible": True,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_reserved_whatsapp_is_recovered_before_external_send_started(self):
        self.assertTrue(
            self.delivery._reserve_delivery(
                self.message_id, self.user_id, "+4512345678"
            )
        )
        sent = []
        self.delivery.client = SimpleNamespace(
            configured=True,
            send_text=lambda phone, text: sent.append((phone, text))
            or {"messageId": "wa-88"},
        )

        original_thread = whatsapp_extension.threading.Thread
        whatsapp_extension.threading.Thread = _ImmediateThread
        try:
            with patch.dict(os.environ, {"PAGER_WHATSAPP_ENABLED": "1"}, clear=False):
                recovered = self.delivery.recover_reserved(max_age_seconds=10**9)
        finally:
            whatsapp_extension.threading.Thread = original_thread

        self.assertEqual(recovered, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "+4512345678")
        row = self.delivery.list_recent(1)[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["openwa_message_id"], "wa-88")

    def test_ambiguous_sending_whatsapp_is_not_retried(self):
        self.assertTrue(
            self.delivery._reserve_delivery(
                self.message_id, self.user_id, "+4512345678"
            )
        )
        self.assertTrue(
            self.delivery._claim_delivery(self.message_id, self.user_id)
        )
        sent = []
        self.delivery.client = SimpleNamespace(
            configured=True,
            send_text=lambda *args, **kwargs: sent.append((args, kwargs)),
        )

        with patch.dict(os.environ, {"PAGER_WHATSAPP_ENABLED": "1"}, clear=False):
            recovered = self.delivery.recover_reserved(max_age_seconds=10**9)

        self.assertEqual(recovered, 0)
        self.assertEqual(sent, [])
        row = self.delivery.list_recent(1)[0]
        self.assertEqual(row["status"], "unknown")
        self.assertIn("undgå dublet", row["error"])


if __name__ == "__main__":
    unittest.main()
