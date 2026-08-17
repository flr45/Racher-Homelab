import os
import tempfile
import unittest

from storage import Storage


class AlarmFeedStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "pager.db"))

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


if __name__ == "__main__":
    unittest.main()
