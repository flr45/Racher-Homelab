from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from alarm_feed_v2 import display_quality_score, promote_duplicate_display
from storage import Storage


class _Adaptive:
    @staticmethod
    def exact_signature(message):
        return "sig:" + str(message)


class _Routing:
    @staticmethod
    def classify(ric, station, message):
        return station or "Næstved", "test"


class AlarmFeedV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)
        self.core = SimpleNamespace(
            storage=self.storage,
            adaptive=_Adaptive(),
            routing=_Routing(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, **overrides):
        payload = {
            "received_at": "2026-08-20T22:39:44",
            "protocol": "POCSAG",
            "baud": 1200,
            "ric": "0006120",
            "station": "Næstved",
            "message": "@0 MN NÆ(1+5)M+S · Park%y4gs9",
            "raw_line": "RAW ORIGINAL",
            "source": "pdl-file",
            "delivery_eligible": True,
            "decision_reason": "første kopi",
        }
        payload.update(overrides)
        return self.storage.add_message(payload)

    def test_clean_complete_copy_scores_above_corrupt_partial_copy(self):
        corrupt = "@0 MN NÆ(1+5)M+S · Park%y4gs9"
        clean = "@0 MN NÆ(1+5)M+S · Parkeringshuset · BRANDALARM · 4700 Næstved"
        self.assertGreater(display_quality_score(clean) - display_quality_score(corrupt), 8.0)

    def test_better_duplicate_promotes_only_public_root_text(self):
        root = self._add()
        clean = "@0 MN NÆ(1+5)M+S · Parkeringshuset · BRANDALARM · 4700 Næstved"
        duplicate = self._add(
            received_at="2026-08-20T22:39:51",
            ric="0009000",
            message=clean,
            raw_line="RAW CLEAN COPY",
            delivery_eligible=False,
            suppressed_reason="duplicate",
            duplicate_of=root,
        )

        self.assertTrue(promote_duplicate_display(self.core, duplicate))

        with self.storage.connect() as conn:
            root_row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (root,)).fetchone())
            duplicate_row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (duplicate,)).fetchone())

        self.assertEqual(root_row["message"], clean)
        self.assertEqual(root_row["raw_line"], "RAW ORIGINAL")
        self.assertEqual(root_row["message_fingerprint"], "sig:" + clean)
        self.assertIn(f"bedre dublet #{duplicate}", root_row["decision_reason"])
        self.assertEqual(duplicate_row["message"], clean)
        self.assertEqual(duplicate_row["raw_line"], "RAW CLEAN COPY")
        self.assertEqual(duplicate_row["duplicate_of"], root)
        self.assertEqual(duplicate_row["delivery_eligible"], 0)

    def test_worse_duplicate_never_replaces_clean_root(self):
        clean = "@0 MN NÆ(1+5)M+S · Parkeringshuset · BRANDALARM · 4700 Næstved"
        root = self._add(message=clean, raw_line="RAW CLEAN ROOT")
        duplicate = self._add(
            received_at="2026-08-20T22:39:51",
            ric="0009000",
            message="@0 MN NÆ(1+5)M+S · Park%y4gs9",
            raw_line="RAW CORRUPT COPY",
            delivery_eligible=False,
            suppressed_reason="duplicate",
            duplicate_of=root,
        )

        self.assertFalse(promote_duplicate_display(self.core, duplicate))
        with self.storage.connect() as conn:
            root_row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (root,)).fetchone())
        self.assertEqual(root_row["message"], clean)
        self.assertEqual(root_row["raw_line"], "RAW CLEAN ROOT")

    def test_transitive_duplicate_promotes_stable_root(self):
        root = self._add(message="4100 Ringsted · lugt af brændt plastic", station="Ringsted")
        middle = self._add(
            ric="0006220",
            station="Ringsted",
            message="4100 Ringsted · lugt af brændt plastic",
            delivery_eligible=False,
            suppressed_reason="duplicate",
            duplicate_of=root,
        )
        best = self._add(
            ric="0006240",
            station="Ringsted",
            message="$9 ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic i lejlighed, ingen synlig røg",
            delivery_eligible=False,
            suppressed_reason="duplicate",
            duplicate_of=middle,
        )

        self.assertTrue(promote_duplicate_display(self.core, best))
        with self.storage.connect() as conn:
            root_row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (root,)).fetchone())
        self.assertTrue(root_row["message"].startswith("$9 ISL-Forespørgsel"))


if __name__ == "__main__":
    unittest.main()
