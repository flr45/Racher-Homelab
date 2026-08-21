from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from ric_noise_filter import RicNoiseFilter, _looks_operational_alarm, install_live_ric_filter
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

    def test_blocked_ric_operational_fragment_remains_reviewable(self):
        rows = [
            {"id": 1, "ric": "0174760", "message": '?P+da?bt?98E?Ø"?. · ISL-ForespørgsedZÆ'},
            {"id": 2, "ric": "0174760", "message": "80804"},
        ]
        filtered = self.filters.filter_review_rows(rows, limit=60)
        self.assertEqual([row["id"] for row in filtered], [1])

    def test_custom_filter_can_be_added_and_removed(self):
        created = self.filters.add("1234567", "Teknisk test", user_id=None)
        self.assertEqual(created["ric"], "1234567")
        self.assertTrue(self.filters.contains("1234567"))
        self.assertTrue(self.filters.remove("1234567"))
        self.assertFalse(self.filters.contains("1234567"))

    def test_invalid_ric_is_rejected(self):
        with self.assertRaises(ValueError):
            self.filters.add("ABC", "støj")

    def test_operational_rescue_requires_strong_alarm_structure(self):
        self.assertTrue(_looks_operational_alarm('?P+da?bt?98E?Ø"?. · ISL-ForespørgsedZÆ'))
        self.assertTrue(_looks_operational_alarm("MN NÆ(1+5)M+S · Næstved"))
        self.assertTrue(_looks_operational_alarm("@5 NR RI(1+5)M+V · Ringsted"))
        self.assertFalse(_looks_operational_alarm("80804"))
        self.assertFalse(_looks_operational_alarm("ISL"))
        self.assertFalse(_looks_operational_alarm("9Zg; · %?A?;??5;AJ%Q'"))

    def test_live_filter_marks_blocked_ric_but_still_calls_raw_ingest(self):
        seen = []

        def original_ingest(event):
            seen.append(event)
            return 42

        core = SimpleNamespace(
            ingest_event=original_ingest,
            app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *args, **kwargs: None)),
        )
        install_live_ric_filter(core, self.filters)

        blocked = SimpleNamespace(ric="0174760", message="40804", decoder_noise_reason=None)
        allowed = SimpleNamespace(ric="0006240", message="Ringsted BRANDALARM", decoder_noise_reason=None)

        self.assertEqual(core.ingest_event(blocked), 42)
        self.assertEqual(blocked.decoder_noise_reason, "ric-filter")
        self.assertEqual(core.ingest_event(allowed), 42)
        self.assertIsNone(allowed.decoder_noise_reason)
        self.assertEqual(seen, [blocked, allowed])

    def test_live_filter_rescues_observed_corrupt_alarm_fragment_on_blocked_ric(self):
        warnings = []

        def original_ingest(event):
            return 77

        core = SimpleNamespace(
            ingest_event=original_ingest,
            app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *args, **kwargs: warnings.append(args))),
        )
        install_live_ric_filter(core, self.filters)

        event = SimpleNamespace(
            ric="0174760",
            message='?P+da?bt?98E?Ø"?. · _hB+da?tq?98E?Ø"?. · ISL-ForespørgsedZÆ',
            decoder_noise_reason=None,
        )
        self.assertEqual(core.ingest_event(event), 77)
        self.assertIsNone(event.decoder_noise_reason)
        self.assertTrue(warnings)

    def test_live_filter_does_not_override_stronger_decoder_noise_reason(self):
        def original_ingest(event):
            return 88

        core = SimpleNamespace(
            ingest_event=original_ingest,
            app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *args, **kwargs: None)),
        )
        install_live_ric_filter(core, self.filters)

        event = SimpleNamespace(
            ric="0174760",
            message="9Zg; · %?A?;??5;AJ%Q'",
            decoder_noise_reason="decoder-gibberish",
        )
        self.assertEqual(core.ingest_event(event), 88)
        self.assertEqual(event.decoder_noise_reason, "decoder-gibberish")


if __name__ == "__main__":
    unittest.main()
