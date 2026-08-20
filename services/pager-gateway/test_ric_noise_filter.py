from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from ric_noise_filter import RicNoiseFilter
from storage import Storage


class RicNoiseFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ric-noise.db")
        self.storage = Storage(self.db)
        self.filters = RicNoiseFilter(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_diagnostic_ric_is_seeded(self):
        self.assertTrue(self.filters.contains("0174760"))
        row = next(item for item in self.filters.list_filters() if item["ric"] == "0174760")
        self.assertIn("diagnostik", row["label"].lower())

    def test_filtered_ric_is_removed_from_learning_rows(self):
        rows = [
            {"id": 1, "ric": "0174760", "message": "40804"},
            {"id": 2, "ric": "0006240", "message": "Ringsted BRANDALARM"},
        ]
        filtered = self.filters.filter_review_rows(rows, limit=60)
        self.assertEqual([row["id"] for row in filtered], [2])

    def test_custom_filter_can_be_added_and_removed(self):
        created = self.filters.add("1234567", "Teknisk test", user_id=None)
        self.assertEqual(created["ric"], "1234567")
        self.assertTrue(self.filters.contains("1234567"))
        self.assertTrue(self.filters.remove("1234567"))
        self.assertFalse(self.filters.contains("1234567"))

    def test_invalid_ric_is_rejected(self):
        with self.assertRaises(ValueError):
            self.filters.add("ABC", "støj")


if __name__ == "__main__":
    unittest.main()
