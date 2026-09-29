from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routing import RoutingStore
from storage import Storage
import whatsapp_extension
from whatsapp_extension import OpenWAClient, WhatsAppDelivery, install_whatsapp


class _Logger:
    def warning(self, *args, **kwargs):
        return None


class _DeferredThread:
    created = []

    def __init__(self, target, args=(), kwargs=None, **_unused):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.__class__.created.append(self)

    def start(self):
        return None


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **_unused):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)




class _JsonResponse:
    def __init__(self, payload):
        import json
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


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


    def test_openwa_health_requires_ready_session_not_just_configuration(self):
        with patch.dict(os.environ, {
            "PAGER_OPENWA_URL": "http://openwa:2785",
            "PAGER_OPENWA_API_KEY": "test-key",
            "PAGER_OPENWA_SESSION": "pager",
        }, clear=False):
            client = OpenWAClient()
            with patch(
                "whatsapp_extension.urllib.request.urlopen",
                return_value=_JsonResponse([
                    {"id": "session-123", "name": "pager", "status": "ready"}
                ]),
            ):
                health = client.session_health(force=True)

        self.assertTrue(health["reachable"])
        self.assertTrue(health["ready"])
        self.assertEqual(health["status"], "ready")

    def test_openwa_qr_ready_is_not_reported_as_connected_whatsapp(self):
        with patch.dict(os.environ, {
            "PAGER_OPENWA_URL": "http://openwa:2785",
            "PAGER_OPENWA_API_KEY": "test-key",
            "PAGER_OPENWA_SESSION": "pager",
        }, clear=False):
            client = OpenWAClient()
            with patch(
                "whatsapp_extension.urllib.request.urlopen",
                return_value=_JsonResponse([
                    {"id": "session-123", "name": "pager", "status": "qr_ready"}
                ]),
            ):
                health = client.session_health(force=True)

        self.assertTrue(health["reachable"])
        self.assertFalse(health["ready"])
        self.assertEqual(health["status"], "qr_ready")

    def test_final_notification_hook_queues_whatsapp_and_preserves_existing_channels(self):
        delivery = install_whatsapp(
            self.app, self.storage, self.routing, self.core.auth_required, core=self.core
        )
        queued = []
        delivery.dispatch_async = lambda message_id, event: queued.append((message_id, dict(event)))

        event = {"message": "Alarm", "station": "A", "delivery_eligible": True}
        self.core.maybe_notify_pushover(42, event)

        self.assertEqual([42], [message_id for message_id, _ in queued])
        self.assertEqual([42], [message_id for message_id, _ in self.original_notifications])

    def test_suppressed_alarm_is_not_queued_for_whatsapp(self):
        delivery = install_whatsapp(
            self.app, self.storage, self.routing, self.core.auth_required, core=self.core
        )
        queued = []
        delivery.dispatch_async = lambda message_id, event: queued.append((message_id, dict(event)))

        event = {"message": "Støj", "delivery_eligible": False}
        self.core.maybe_notify_pushover(43, event)

        self.assertEqual([], queued)
        self.assertEqual([43], [message_id for message_id, _ in self.original_notifications])





    def test_mock_whatsapp_alarm_is_clearly_marked_as_test(self):
        text = WhatsAppDelivery.format_alarm({
            "station": "Ringsted",
            "message": "BRANDALARM Ringsted",
            "source": "mock",
        })
        self.assertTrue(text.startswith("🧪 TESTALARM – Ringsted"))

    def test_user_whatsapp_alarm_never_contains_ric_metadata(self):
        text = WhatsAppDelivery.format_alarm({
            "station": "Ringsted",
            "message": "BRANDALARM Ringsted",
            "ric": "0006240",
            "received_at": "2026-09-29T06:00:00+00:00",
        })
        self.assertIn("BRANDALARM Ringsted", text)
        self.assertNotIn("0006240", text)
        self.assertNotIn("RIC", text)


    def test_whatsapp_utc_timestamp_is_rendered_in_copenhagen_time(self):
        with patch.dict(os.environ, {"PAGER_LOCAL_TIMEZONE": "Europe/Copenhagen"}, clear=False):
            text = WhatsAppDelivery.format_alarm({
                "station": "Ringsted",
                "message": "BRANDALARM Ringsted",
                "received_at": "2026-09-29T06:00:00+00:00",
            })
        self.assertIn("29-09-2026 08:00:00", text)

    def _direct_delivery(self):
        user_id = self.storage.create_user(
            "tester", "Tester", "unused-test-hash", "user"
        )
        message_id = self.storage.add_message({
            "received_at": "2026-09-29T06:00:00+00:00",
            "protocol": "POCSAG",
            "baud": 1200,
            "ric": "0006240",
            "station": "Ringsted",
            "message": "BRANDALARM Ringsted",
            "raw_line": "raw",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        delivery = WhatsAppDelivery(
            SimpleNamespace(logger=_Logger()),
            self.storage,
            self.routing,
        )
        return delivery, user_id, message_id

    def test_async_dispatch_reserves_before_background_worker_runs(self):
        with patch.dict(os.environ, {
            "PAGER_WHATSAPP_ENABLED": "1",
            "PAGER_OPENWA_URL": "http://openwa:2785",
            "PAGER_OPENWA_API_KEY": "test-key",
            "PAGER_OPENWA_SESSION": "pager",
        }, clear=False):
            delivery, user_id, message_id = self._direct_delivery()
            delivery.recipients_for_event = lambda _station: [{
                "user_id": user_id,
                "display_name": "Tester",
                "phone_e164": "+4512345678",
            }]
            _DeferredThread.created = []
            original_thread = whatsapp_extension.threading.Thread
            whatsapp_extension.threading.Thread = _DeferredThread
            try:
                queued = delivery.dispatch_async(message_id, {
                    "received_at": "2026-09-29T06:00:00+00:00",
                    "ric": "0006240",
                    "station": "Ringsted",
                    "message": "BRANDALARM Ringsted",
                    "source": "pdl-file",
                    "delivery_eligible": True,
                })
            finally:
                whatsapp_extension.threading.Thread = original_thread

        self.assertEqual(queued, 1)
        self.assertEqual(len(_DeferredThread.created), 1)
        row = delivery.list_recent()[0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["message_id"], message_id)

    def test_restart_recovers_durably_queued_whatsapp(self):
        with patch.dict(os.environ, {
            "PAGER_WHATSAPP_ENABLED": "1",
            "PAGER_OPENWA_URL": "http://openwa:2785",
            "PAGER_OPENWA_API_KEY": "test-key",
            "PAGER_OPENWA_SESSION": "pager",
        }, clear=False):
            delivery, user_id, message_id = self._direct_delivery()
            self.assertTrue(
                delivery._reserve_delivery(message_id, user_id, "+4512345678")
            )
            sent = []
            delivery.client.send_text = lambda phone, text: sent.append(
                (phone, text)
            ) or {"messageId": "wa-101"}
            original_thread = whatsapp_extension.threading.Thread
            whatsapp_extension.threading.Thread = _ImmediateThread
            try:
                result = delivery.recover_after_restart()
            finally:
                whatsapp_extension.threading.Thread = original_thread

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["uncertain"], 0)
        self.assertEqual(len(sent), 1)
        row = delivery.list_recent()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["openwa_message_id"], "wa-101")

    def test_restart_does_not_blindly_resend_inflight_whatsapp(self):
        with patch.dict(os.environ, {
            "PAGER_WHATSAPP_ENABLED": "1",
            "PAGER_OPENWA_URL": "http://openwa:2785",
            "PAGER_OPENWA_API_KEY": "test-key",
            "PAGER_OPENWA_SESSION": "pager",
        }, clear=False):
            delivery, user_id, message_id = self._direct_delivery()
            self.assertTrue(
                delivery._reserve_delivery(message_id, user_id, "+4512345678")
            )
            self.assertTrue(delivery._mark_sending(message_id, user_id))
            delivery.client.send_text = lambda *_args, **_kwargs: self.fail(
                "in-flight WhatsApp delivery must not be blindly resent"
            )
            result = delivery.recover_after_restart()

        self.assertEqual(result["recovered"], 0)
        self.assertEqual(result["uncertain"], 1)
        row = delivery.list_recent()[0]
        self.assertEqual(row["status"], "uncertain")
        self.assertIn("undgå dubletbesked", row["error"])

if __name__ == "__main__":
    unittest.main()
