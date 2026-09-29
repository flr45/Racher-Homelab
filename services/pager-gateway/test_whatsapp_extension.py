from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from routing import RoutingStore
from storage import Storage
from whatsapp_extension import install_whatsapp


class WhatsAppExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.tmp.name) / "pager.db")
        self.storage = Storage(db_path)
        self.routing = RoutingStore(db_path)
        self.app = Flask(__name__)
        self.original_notifications = []

        def auth_required(admin: bool = False):
            del admin
            return lambda fn: fn

        def original_notify(message_id, event):
            self.original_notifications.append((message_id, dict(event)))

        self.core = SimpleNamespace(
            app=self.app,
            storage=self.storage,
            routing=self.routing,
            auth_required=auth_required,
            maybe_notify_pushover=original_notify,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_final_notification_hook_queues_whatsapp_and_preserves_existing_channels(self):
        delivery = install_whatsapp(self.core)
        queued = []
        delivery.dispatch_async = lambda message_id, event: queued.append((message_id, dict(event)))

        event = {"message": "Alarm", "station": "A", "delivery_eligible": True}
        self.core.maybe_notify_pushover(42, event)

        self.assertEqual([42], [message_id for message_id, _ in queued])
        self.assertEqual([42], [message_id for message_id, _ in self.original_notifications])

    def test_suppressed_alarm_is_not_queued_for_whatsapp(self):
        delivery = install_whatsapp(self.core)
        queued = []
        delivery.dispatch_async = lambda message_id, event: queued.append((message_id, dict(event)))

        event = {"message": "Støj", "delivery_eligible": False}
        self.core.maybe_notify_pushover(43, event)

        self.assertEqual([], queued)
        self.assertEqual([43], [message_id for message_id, _ in self.original_notifications])


if __name__ == "__main__":
    unittest.main()
