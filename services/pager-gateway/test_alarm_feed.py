import os
import tempfile
import unittest

from storage import Storage


class AlarmFeedStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_feed_only_returns_delivery_eligible_messages(self):
        alarm_id = self.storage.add_message({
            "received_at": "2026-08-17T15:20:00+00:00",
            "message": "BRANDALARM Testvej 1",
            "raw_line": "BRANDALARM Testvej 1",
            "source": "test",
            "delivery_eligible": True,
        })
        noise_id = self.storage.add_message({
            "received_at": "2026-08-17T15:20:01+00:00",
            "message": "TONE ONLY",
            "raw_line": "TONE ONLY",
            "source": "test",
            "delivery_eligible": False,
            "suppressed_reason": "decoder-mode",
        })

        raw_history = self.storage.list_messages(limit=10)
        live_feed = self.storage.list_messages(limit=10, delivery_eligible_only=True)

        self.assertEqual([row["id"] for row in raw_history], [noise_id, alarm_id])
        self.assertEqual([row["id"] for row in live_feed], [alarm_id])
        self.assertEqual(live_feed[0]["message"], "BRANDALARM Testvej 1")

    def test_latest_message_remains_raw_for_admin_diagnostics(self):
        self.storage.add_message({
            "message": "BRANDALARM Testvej 1",
            "raw_line": "BRANDALARM Testvej 1",
            "source": "test",
            "delivery_eligible": True,
        })
        self.storage.add_message({
            "message": "40804",
            "raw_line": "40804",
            "source": "test",
            "delivery_eligible": False,
            "suppressed_reason": "decoder-code",
        })

        self.assertEqual(self.storage.latest_message()["message"], "40804")
        self.assertEqual(
            self.storage.list_messages(limit=1, delivery_eligible_only=True)[0]["message"],
            "BRANDALARM Testvej 1",
        )

    def test_upgrade_reclassifies_old_decoder_artifacts_without_deleting_history(self):
        alarm_id = self.storage.add_message({
            "message": "$6 ISL KA, KB V1 Naturbrand-Mark",
            "raw_line": "$6 ISL KA, KB V1 Naturbrand-Mark",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        tone_id = self.storage.add_message({
            "message": "TONE ONLY",
            "raw_line": "TONE ONLY",
            "source": "pdl-file",
            "delivery_eligible": True,
        })
        code_id = self.storage.add_message({
            "message": "40Å04",
            "raw_line": "40]04",
            "source": "pdl-file",
            "delivery_eligible": True,
        })

        upgraded = Storage(self.db)
        feed_ids = [row["id"] for row in upgraded.list_messages(10, delivery_eligible_only=True)]
        history = {row["id"]: row for row in upgraded.list_messages(10)}

        self.assertEqual(feed_ids, [alarm_id])
        self.assertIn(tone_id, history)
        self.assertIn(code_id, history)
        self.assertEqual(history[tone_id]["suppressed_reason"], "decoder-mode")
        self.assertEqual(history[code_id]["suppressed_reason"], "decoder-code")


if __name__ == "__main__":
    unittest.main()
