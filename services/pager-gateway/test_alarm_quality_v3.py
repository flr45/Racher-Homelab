from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from alarm_rules import (
    AlarmFilterStore,
    _clean_pager_message,
    _find_extended_duplicate,
    _quality_noise_reason,
)
from storage import Storage


class PagerQualityV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)
        self.filters = AlarmFilterStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, message: str, when: datetime, ric: str) -> int:
        return self.storage.add_message({
            "received_at": when.isoformat(),
            "protocol": "POCSAG",
            "baud": 1200,
            "ric": ric,
            "message": message,
            "raw_line": message,
            "source": "test",
            "delivery_eligible": True,
        })

    def test_known_decoder_prefixes_are_removed_from_display_text(self):
        self.assertEqual(
            _clean_pager_message("$9 ISL-Forespørgsel · 4100 Ringsted"),
            "ISL-Forespørgsel · 4100 Ringsted",
        )
        self.assertEqual(
            _clean_pager_message("@7 NR RI(1+5)M+S · Ringstedet · BRANDALARM"),
            "NR RI(1+5)M+S · Ringstedet · BRANDALARM",
        )
        self.assertEqual(
            _clean_pager_message("?4100 Ringsted · lugt af brændt plastic"),
            "4100 Ringsted · lugt af brændt plastic",
        )

    def test_tiny_corrupt_fragment_is_suppressed(self):
        self.assertEqual(_quality_noise_reason("$9 i?"), "decoder-fragment")
        self.assertEqual(_quality_noise_reason("/"), "decoder-fragment")
        self.assertIsNone(_quality_noise_reason("BRANDALARM"))

    def test_same_incident_repeated_across_rics_after_46_seconds_is_duplicate(self):
        now = datetime.now(timezone.utc)
        first = self._add(
            "ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic i lejlighed, ingen synlig røg eller f",
            now,
            "0009000",
        )
        duplicate = _find_extended_duplicate(
            self.filters,
            "4100 Ringsted · lugt af brændt plastic i lejlighed, ingen synlig røg eller f",
            (now + timedelta(seconds=44)).isoformat(),
        )
        self.assertEqual(duplicate, first)

    def test_leading_decoder_prefix_does_not_defeat_extended_dedupe(self):
        now = datetime.now(timezone.utc)
        first = self._add(
            "ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic i lejlighed, ingen synlig røg eller f",
            now,
            "0009000",
        )
        duplicate = _find_extended_duplicate(
            self.filters,
            "$9 ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic i lejlighed, ingen synlig røg eller f",
            (now + timedelta(seconds=46)).isoformat(),
        )
        self.assertEqual(duplicate, first)

    def test_different_incident_same_postcode_is_not_suppressed(self):
        now = datetime.now(timezone.utc)
        self._add(
            "ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic i lejlighed, ingen synlig røg",
            now,
            "0009000",
        )
        duplicate = _find_extended_duplicate(
            self.filters,
            "ISL-Forespørgsel · 4100 Ringsted · færdselsuheld med fastklemt person på motorvejen",
            (now + timedelta(seconds=40)).isoformat(),
        )
        self.assertIsNone(duplicate)


if __name__ == "__main__":
    unittest.main()
