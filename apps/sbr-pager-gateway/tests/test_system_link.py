from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


class _FakeBridge:
    def send_text(self, _phone: str, _body: str) -> str:
        raise AssertionError("WhatsApp må ikke kaldes før delay er udløbet")


class SystemLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = self.temp.name

        import build_config
        import storage
        import station_events
        import system_link

        self.build_config = build_config
        self.storage = storage
        self.station_events = station_events
        self.system_link = system_link
        storage.init_database()
        station_events.ensure_station_event_schema()
        system_link.ensure_system_link_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _accepted_message(self, source_id: str = "system-link-test") -> int:
        message_id, _ = self.storage.store_inbound_message(
            source_id=source_id,
            sender="+4599999999",
            body="Alarm (S) · Bygningsbrand · Testvej 1, 4200 Slagelse",
            received_at="2026-09-17T16:00:00Z",
            modem_port="COM9",
            processing_status="accepted_pending_whatsapp",
        )
        self.storage.set_message_status(
            message_id,
            "accepted_pending_whatsapp",
            accepted=True,
        )
        self.station_events.record_alarm_message(
            message_id,
            sender="+4599999999",
            body="Alarm (S) · Bygningsbrand · Testvej 1, 4200 Slagelse",
            received_at="2026-09-17T16:00:00Z",
        )
        return message_id

    def test_system_link_queues_and_sends_accepted_alarm_legacy_mode(self) -> None:
        self.storage.set_setting("system_link_started_at", "2000-01-01T00:00:00Z")
        message_id = self._accepted_message()
        self.storage.set_setting("system_link_enabled", "1")
        self.storage.set_setting("system_link_endpoint", "https://example.invalid/gateway")
        self.storage.set_setting("system_link_token", "test-secret")

        engine = self.system_link.SystemLinkEngine()
        engine._queue_accepted_messages()

        with patch.object(self.system_link, "_request") as request_mock:
            engine._process_one()

        request_mock.assert_called_once()
        payload = request_mock.call_args.args[0]
        self.assertEqual(payload["message"]["id"], message_id)
        self.assertEqual(payload["message"]["body"], "Alarm (S) · Bygningsbrand · Testvej 1, 4200 Slagelse")
        self.assertEqual(payload["event"]["station"], "S")
        self.assertEqual(payload["deliveryId"], "legacy:system-link-test")

        with self.storage.connection() as db:
            row = db.execute(
                "SELECT status, attempts, delivered_at FROM system_link_deliveries WHERE inbound_id=?",
                (message_id,),
            ).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(int(row["attempts"]), 1)
        self.assertTrue(row["delivered_at"])

    @unittest.skipUnless(os.name == "nt", "DPAPI provisioning test requires Windows")
    def test_installer_bootstrap_provisions_and_enables_system_link(self) -> None:
        with (
            patch.object(self.build_config, "SYSTEM_LINK_PROVISION_URL", "https://link.example.test/api/provision"),
            patch.object(self.build_config, "SYSTEM_LINK_PROVISION_TOKEN", "single-use-token"),
            patch.object(self.build_config, "SYSTEM_LINK_MESSAGE_ENDPOINT", "https://link.example.test/api/system-link"),
            patch.object(self.system_link, "_json_request") as request_mock,
        ):
            request_mock.return_value = {
                "ok": True,
                "clientId": "gw-test123",
                "clientSecret": "client-secret-123",
                "messageEndpoint": "https://link.example.test/api/system-link",
            }
            credentials = self.system_link.ensure_provisioned()

        self.assertIsNotNone(credentials)
        self.assertEqual(credentials["client_id"], "gw-test123")
        self.assertEqual(self.storage.get_setting("system_link_enabled"), "1")
        status = self.system_link.system_link_status()
        self.assertTrue(status["provisioned"])
        self.assertEqual(status["client_id"], "gw-test123")
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.args[1]["token"], "single-use-token")

    def test_system_link_does_not_replay_messages_before_activation(self) -> None:
        message_id = self._accepted_message("old-before-link")
        self.storage.set_setting("system_link_started_at", "2099-01-01T00:00:00Z")
        engine = self.system_link.SystemLinkEngine()
        engine._queue_accepted_messages()

        with self.storage.connection() as db:
            row = db.execute(
                "SELECT id FROM system_link_deliveries WHERE inbound_id=?",
                (message_id,),
            ).fetchone()
        self.assertIsNone(row)

    def test_sms_forward_delay_keeps_message_pending(self) -> None:
        from delivery import WhatsAppDeliveryEngine

        message_id = self._accepted_message("delay-test")
        self.storage.set_setting("sms_forward_delay_seconds", "300")
        engine = WhatsAppDeliveryEngine(_FakeBridge())
        engine._process_batch()

        with self.storage.connection() as db:
            row = db.execute(
                "SELECT processing_status FROM inbound_messages WHERE id=?",
                (message_id,),
            ).fetchone()
        self.assertEqual(row["processing_status"], "accepted_pending_whatsapp")


if __name__ == "__main__":
    unittest.main()
