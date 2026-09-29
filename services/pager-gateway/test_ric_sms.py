from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

import ric_sms
from ric_sms import (
    RicSmsRouter,
    RicSmsStore,
    format_alarm_sms,
    format_ric_call_sms,
    normalize_phone,
)
from storage import Storage


class _Logger:
    def warning(self, *args, **kwargs):
        return None


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **_unused):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class RicSmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_danish_phone_normalization(self):
        self.assertEqual(normalize_phone("12 34 56 78"), "+4512345678")
        self.assertEqual(normalize_phone("004512345678"), "+4512345678")
        with self.assertRaises(ValueError):
            normalize_phone("123")

    def test_sms_is_kept_single_part(self):
        text = format_alarm_sms({"station": "Ringsted", "message": "X" * 400})
        self.assertLessEqual(len(text), 160)
        self.assertTrue(text.startswith("RACHER PAGER\nRingsted\n"))

    def test_ric_call_sms_is_generic_and_single_part(self):
        text = format_ric_call_sms(
            {"station": "Slagelse", "ric": "0001133", "message": "12345"},
            "0001133",
        )
        self.assertEqual(text, "RACHER PAGER\nSlagelse\nRIC 0001133 kaldt")
        self.assertLessEqual(len(text), 160)

    def test_rules_are_persistent(self):
        store = RicSmsStore(self.db)
        rule = store.add_rule("0006240", "12345678", "Vagttelefon")
        self.assertEqual(rule["phone"], "+4512345678")
        self.assertTrue(rule["active"])
        rules = store.rules_for_rics({"0006240"})
        self.assertEqual(len(rules), 1)
        updated = store.update_rule(rule["id"], active=False)
        self.assertFalse(updated["active"])
        self.assertEqual(store.rules_for_rics({"0006240"}), [])

    def test_multi_ric_burst_sends_once_per_phone(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        router.store.add_rule("0006210", "+4512345678", "Første RIC")
        router.store.add_rule("0006240", "+4512345678", "Anden RIC")

        base_event = {
            "received_at": "2026-08-20T12:35:35+00:00",
            "protocol": "POCSAG",
            "baud": 1200,
            "function": "1",
            "station": "Ringsted",
            "message": "@8 NR RI(1+5)M+S Ringsted Svømmeland BRANDALARM 4100 Ringsted",
            "raw_line": "raw",
            "source": "pdl-file",
            "delivery_eligible": True,
        }
        representative = self.storage.add_message({**base_event, "ric": "0006210"})
        duplicate = self.storage.add_message({
            **base_event,
            "ric": "0006240",
            "delivery_eligible": False,
            "suppressed_reason": "duplicate",
            "duplicate_of": representative,
        })
        self.assertGreater(duplicate, representative)

        sent = []
        router._post_outgoing = lambda gateway_url, recipient, body: sent.append(
            (gateway_url, recipient, body)
        ) or {"id": 77, "status": "pending"}

        original_thread = ric_sms.threading.Thread
        ric_sms.threading.Thread = _ImmediateThread
        try:
            queued = router.queue_for_event(representative, {**base_event, "ric": "0006210"})
            queued_again = router.queue_for_event(representative, {**base_event, "ric": "0006210"})
        finally:
            ric_sms.threading.Thread = original_thread

        self.assertEqual(queued, 1)
        self.assertEqual(queued_again, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "+4512345678")
        delivery = router.store.list_deliveries()[0]
        self.assertEqual(delivery["status"], "queued")
        self.assertEqual(set(delivery["matched_rics"].split(",")), {"0006210", "0006240"})

    def test_reserved_sms_is_recovered_after_restart_before_external_send(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")

        event = {
            "received_at": "2026-09-29T10:00:00+00:00",
            "protocol": "POCSAG",
            "baud": 1200,
            "ric": "0006240",
            "function": "1",
            "station": "Ringsted",
            "message": "BRANDALARM Testvej 1",
            "raw_line": "raw",
            "source": "pdl-file",
            "delivery_eligible": True,
        }
        message_id = self.storage.add_message(event)
        self.assertTrue(
            router.store.reserve_delivery(message_id, "+4512345678", {"0006240"})
        )

        sent = []
        router._post_outgoing = lambda gateway_url, recipient, body: sent.append(
            (gateway_url, recipient, body)
        ) or {"id": 88, "status": "pending"}

        original_thread = ric_sms.threading.Thread
        ric_sms.threading.Thread = _ImmediateThread
        try:
            recovered = router.recover_reserved(max_age_seconds=10**9)
        finally:
            ric_sms.threading.Thread = original_thread

        self.assertEqual(recovered, 1)
        self.assertEqual(len(sent), 1)
        delivery = router.store.list_deliveries()[0]
        self.assertEqual(delivery["status"], "queued")
        self.assertEqual(delivery["gateway_message_id"], "88")

    def test_remote_gateway_sent_status_is_mirrored_locally(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        message_id = self.storage.add_message({
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
        self.assertTrue(
            router.store.reserve_delivery(message_id, "+4512345678", {"0006240"})
        )
        self.assertTrue(router.store.claim_delivery(message_id, "+4512345678"))
        router.store.finish_delivery(
            message_id,
            "+4512345678",
            status="queued",
            gateway_message_id="88",
        )
        router._get_outgoing_status = lambda gateway_url, remote_id: {
            "id": 88,
            "status": "sent",
            "error": None,
        }

        changed = router.reconcile_remote_statuses()

        self.assertEqual(changed, 1)
        delivery = router.store.list_deliveries()[0]
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(delivery["gateway_message_id"], "88")
        self.assertIsNone(delivery["error"])

    def test_remote_gateway_failure_is_mirrored_without_retry(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        message_id = self.storage.add_message({
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
        self.assertTrue(
            router.store.reserve_delivery(message_id, "+4512345678", {"0006240"})
        )
        self.assertTrue(router.store.claim_delivery(message_id, "+4512345678"))
        router.store.finish_delivery(
            message_id,
            "+4512345678",
            status="queued",
            gateway_message_id="89",
        )
        router._get_outgoing_status = lambda gateway_url, remote_id: {
            "id": 89,
            "status": "failed",
            "error": "SMS-afsendelse fik ikke kvittering fra modem",
        }

        changed = router.reconcile_remote_statuses()

        self.assertEqual(changed, 1)
        delivery = router.store.list_deliveries()[0]
        self.assertEqual(delivery["status"], "failed")
        self.assertIn("ikke kvittering", delivery["error"])

    def test_ambiguous_sending_sms_is_not_retried_after_restart(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        message_id = self.storage.add_message({
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
        self.assertTrue(
            router.store.reserve_delivery(message_id, "+4512345678", {"0006240"})
        )
        self.assertTrue(router.store.claim_delivery(message_id, "+4512345678"))

        sent = []
        router._post_outgoing = lambda *args, **kwargs: sent.append((args, kwargs)) or {"id": 99}
        recovered = router.recover_reserved(max_age_seconds=10**9)

        self.assertEqual(recovered, 0)
        self.assertEqual(sent, [])
        delivery = router.store.list_deliveries()[0]
        self.assertEqual(delivery["status"], "unknown")
        self.assertIn("undgå dublet", delivery["error"])

    def test_ric_call_fallback_dedupes_and_blocks_later_alarm_for_same_ric(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        router.store.add_rule("0006240", "+4512345678", "Vagttelefon")

        call_event = {
            "received_at": "2026-09-29T12:00:00+00:00",
            "protocol": "POCSAG",
            "baud": 1200,
            "ric": "0006240",
            "station": "Ringsted",
            "message": "123456",
            "raw_line": "numeric raw",
            "source": "pdl-file",
            "delivery_eligible": False,
            "suppressed_reason": "decoder-non-alpha",
        }
        first_id = self.storage.add_message(call_event)
        sent = []
        router._post_outgoing = lambda gateway_url, recipient, body: sent.append(
            (gateway_url, recipient, body)
        ) or {"id": 101, "status": "pending"}

        original_thread = ric_sms.threading.Thread
        ric_sms.threading.Thread = _ImmediateThread
        try:
            self.assertEqual(router.queue_for_ric_call(first_id, call_event), 1)

            second_id = self.storage.add_message({**call_event, "raw_line": "numeric raw 2"})
            self.assertEqual(router.queue_for_ric_call(second_id, call_event), 0)

            alarm_event = {
                **call_event,
                "message": "BRANDALARM Testvej 1",
                "delivery_eligible": True,
                "suppressed_reason": None,
            }
            alarm_id = self.storage.add_message(alarm_event)
            self.assertEqual(router.queue_for_event(alarm_id, alarm_event), 0)
        finally:
            ric_sms.threading.Thread = original_thread

        self.assertEqual(len(sent), 1)
        self.assertIn("RIC 0006240 kaldt", sent[0][2])
        delivery = router.store.list_deliveries()[0]
        self.assertEqual(delivery["trigger_kind"], "ric-call")
        self.assertEqual(delivery["status"], "queued")

    def test_explicit_word_filter_still_blocks_ric_call_fallback(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        router.store.add_rule("0006240", "+4512345678", "Vagttelefon")
        event = {
            "source": "pdl-file",
            "ric": "0006240",
            "station": "Ringsted",
            "message": "TEST",
            "delivery_eligible": False,
            "suppressed_reason": "word-filter:TEST",
        }
        message_id = self.storage.add_message(event)
        self.assertEqual(router.queue_for_ric_call(message_id, event), 0)
        self.assertEqual(router.store.list_deliveries(), [])

    def test_simulator_never_sends_sms(self):
        core = SimpleNamespace(
            DB_PATH=self.db,
            storage=self.storage,
            maybe_notify_pushover=lambda message_id, event: None,
            app=SimpleNamespace(logger=_Logger()),
        )
        router = RicSmsRouter(core)
        router.store.update_config(enabled=True, gateway_url="http://sms-gateway:8090")
        router.store.add_rule("0006240", "+4512345678", "Test")
        self.assertEqual(router.queue_for_event(1, {
            "source": "mock",
            "ric": "0006240",
            "message": "Testalarm",
            "station": "Ringsted",
            "delivery_eligible": True,
        }), 0)


if __name__ == "__main__":
    unittest.main()
