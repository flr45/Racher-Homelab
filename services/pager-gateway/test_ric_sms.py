from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

import ric_sms
from ric_sms import RicSmsRouter, RicSmsStore, format_alarm_sms, normalize_phone
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
