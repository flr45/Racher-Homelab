from __future__ import annotations

import os
import tempfile
import unittest

from operations import OperationsStore
from storage import Storage


class OperationsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upgrade_keeps_historic_rows_out_of_current_alarm_window(self):
        historic = self.storage.add_message({
            "message": "BRANDALARM før operations-opgradering",
            "raw_line": "BRANDALARM før operations-opgradering",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        ops = OperationsStore(self.db, current_alarm_minutes=120)
        self.assertNotIn(historic, ops.current_message_ids())

        live = self.storage.add_message({
            "message": "BRANDALARM efter operations-opgradering",
            "raw_line": "BRANDALARM efter operations-opgradering",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        self.assertIn(live, ops.current_message_ids())

    def test_delivery_status_round_trip_and_public_error_redaction(self):
        ops = OperationsStore(self.db)
        message_id = self.storage.add_message({
            "message": "BRANDALARM Testvej 1",
            "raw_line": "BRANDALARM Testvej 1",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        ops.record_delivery(
            message_id, "pushover", "failed",
            target_count=1, failed_count=1, latency_ms=812,
            last_error="hemmelig upstream detalje",
        )

        admin_rows = ops.attach_delivery(self.storage.list_messages(10), include_errors=True)
        public_rows = ops.attach_delivery(self.storage.list_messages(10), include_errors=False)
        self.assertEqual(admin_rows[0]["delivery"]["pushover"]["status"], "failed")
        self.assertEqual(admin_rows[0]["delivery"]["pushover"]["latency_ms"], 812)
        self.assertIn("last_error", admin_rows[0]["delivery"]["pushover"])
        self.assertNotIn("last_error", public_rows[0]["delivery"]["pushover"])

    def test_quality_counts_noise_duplicates_fragments_and_question_marks(self):
        ops = OperationsStore(self.db)
        self.storage.add_message({
            "message": "BRANDALARM H?stet",
            "raw_line": "BRANDALARM H?stet",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        original = self.storage.add_message({
            "message": "BRANDALARM Testvej 1",
            "raw_line": "BRANDALARM Testvej 1",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        self.storage.add_message({
            "message": "BRANDALARM Testvej 1",
            "raw_line": "BRANDALARM Testvej 1",
            "source": "pdl-file",
            "delivery_eligible": False,
            "duplicate_of": original,
            "suppressed_reason": "duplicate",
        })
        self.storage.add_message({
            "message": "førerhus, spredt sig",
            "raw_line": "førerhus, spredt sig",
            "source": "pdl-file",
            "delivery_eligible": False,
            "relevance_class": "noise",
            "suppressed_reason": "decoder-fragment",
        })

        quality = ops.quality(1)
        self.assertEqual(quality["raw_count"], 4)
        self.assertEqual(quality["accepted_count"], 2)
        self.assertEqual(quality["suppressed_count"], 2)
        self.assertEqual(quality["duplicate_count"], 1)
        self.assertEqual(quality["fragment_count"], 1)
        self.assertEqual(quality["question_marks"], 1)


if __name__ == "__main__":
    unittest.main()
