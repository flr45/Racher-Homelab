from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from adaptive import AdaptiveFilter
from storage import Storage


class AdaptiveFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "adaptive.db")
        self.storage = Storage(self.db)
        self.adaptive = AdaptiveFilter(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, text: str, when: datetime, ric: str = "1111111", **extra) -> int:
        decision = self.adaptive.evaluate(
            text,
            when.isoformat(),
            30,
            ric=ric,
            function=extra.get("function"),
        )
        payload = {
            "received_at": when.isoformat(),
            "protocol": "POCSAG",
            "ric": ric,
            "message": text,
            "raw_line": text,
            "source": "test",
            **decision,
            **extra,
        }
        message_id = self.storage.add_message(payload)
        self.adaptive.observe(message_id, text)
        return message_id

    def test_immediate_identical_message_is_duplicate_even_with_different_ric(self):
        now = datetime.now(timezone.utc)
        first = self._add("BRANDALARM Testvej 1", now, "1111111")
        decision = self.adaptive.evaluate("BRANDALARM Testvej 1", (now + timedelta(seconds=5)).isoformat(), 30)
        self.assertFalse(decision["delivery_eligible"])
        self.assertEqual(decision["suppressed_reason"], "duplicate")
        self.assertEqual(decision["duplicate_of"], first)

    def test_non_consecutive_same_message_is_not_immediate_duplicate(self):
        now = datetime.now(timezone.utc)
        self._add("BRANDALARM Testvej 1", now)
        self._add("ANDEN MELDING", now + timedelta(seconds=2), "2222222")
        decision = self.adaptive.evaluate("BRANDALARM Testvej 1", (now + timedelta(seconds=4)).isoformat(), 30)
        self.assertTrue(decision["delivery_eligible"])
        self.assertIsNone(decision["duplicate_of"])

    def test_unknown_pattern_defaults_to_delivery(self):
        decision = self.adaptive.evaluate("Ny ukendt alarmtype", datetime.now(timezone.utc).isoformat(), 30)
        self.assertTrue(decision["delivery_eligible"])
        self.assertEqual(decision["relevance_class"], "unknown")

    def test_observed_decoder_gibberish_is_suppressed(self):
        samples = [
            'WH?U*e?rp?98E?Ø"? · WHC+da?rp?98E?Ø"?',
            '*UJ+da?rp?98E?Ø"?. · WHC+da?rp?98E?',
            '??P+dA?Us?98E?Ø&?.??',
            'W',
            '/',
            '*U*U*U*U*U*U*U*U*U*U',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                decision = self.adaptive.evaluate(sample, datetime.now(timezone.utc).isoformat(), 30)
                self.assertFalse(decision["delivery_eligible"])
                self.assertTrue(str(decision["suppressed_reason"]).startswith("decoder-"))

    def test_raw_pdl_header_is_suppressed(self):
        text = "0174760 05:17:51 20-08-26 POCSAG-1 ALPHA 1200"
        decision = self.adaptive.evaluate(text, datetime.now(timezone.utc).isoformat(), 30)
        self.assertFalse(decision["delivery_eligible"])
        self.assertEqual(decision["suppressed_reason"], "decoder-header")

    def test_alarm_marker_survives_partial_decoder_corruption(self):
        text = "@7 NR RI(1+5)M+S??Ringstedet??BRANDALARM??4100 Ringsted"
        decision = self.adaptive.evaluate(text, datetime.now(timezone.utc).isoformat(), 30, ric="0006240")
        self.assertTrue(decision["delivery_eligible"])
        self.assertIsNone(decision["suppressed_reason"])

    def test_near_simultaneous_alarm_variant_is_duplicate_across_rics(self):
        now = datetime.now(timezone.utc)
        first = self._add(
            "@7 NR RI(1+5)M+S??Ringstedet??BRANDALARM??4100 Ringsted",
            now,
            "0006240",
        )
        decision = self.adaptive.evaluate(
            "@7 NR ri*1+5)M+S??Ringstedet??BRANDALARM??4100 Ringsted",
            (now + timedelta(seconds=1)).isoformat(),
            30,
            ric="0005300",
        )
        self.assertFalse(decision["delivery_eligible"])
        self.assertEqual(decision["suppressed_reason"], "duplicate")
        self.assertEqual(decision["duplicate_of"], first)

    def test_three_noise_votes_teach_exact_noise_pattern(self):
        now = datetime.now(timezone.utc)
        ids = []
        for index in range(3):
            # Separate with other messages so duplicate suppression does not matter for learning.
            ids.append(self._add("FAST TEKNISK TESTMELDING", now + timedelta(minutes=index * 2)))
            if index < 2:
                self._add(f"separator {index}", now + timedelta(minutes=index * 2, seconds=1))
        for message_id in ids:
            self.adaptive.record_feedback(message_id, "noise", None)
        learned = self.adaptive.learned_relevance("FAST TEKNISK TESTMELDING")
        self.assertEqual(learned["classification"], "noise")
        decision = self.adaptive.evaluate("FAST TEKNISK TESTMELDING", (now + timedelta(hours=1)).isoformat(), 30)
        self.assertFalse(decision["delivery_eligible"])
        self.assertEqual(decision["suppressed_reason"], "noise")

    def test_relevant_feedback_prevents_unanimous_noise_learning(self):
        now = datetime.now(timezone.utc)
        ids = []
        for index in range(4):
            ids.append(self._add("TVETYDIG MELDING", now + timedelta(minutes=index * 2)))
            if index < 3:
                self._add(f"separator relevant {index}", now + timedelta(minutes=index * 2, seconds=1))
        self.adaptive.record_feedback(ids[0], "relevant", None)
        for message_id in ids[1:]:
            self.adaptive.record_feedback(message_id, "noise", None)
        learned = self.adaptive.learned_relevance("TVETYDIG MELDING")
        self.assertNotEqual(learned["classification"], "noise")

    def test_feedback_on_legacy_message_creates_missing_patterns(self):
        text = "ÆLDRE HISTORISK TESTMELDING"
        message_id = self.storage.add_message({
            "received_at": datetime.now(timezone.utc).isoformat(),
            "protocol": "POCSAG",
            "ric": "5555555",
            "message": text,
            "raw_line": text,
            "source": "legacy-test",
        })
        # Simulate a message that predates adaptive.observe()/adaptive_patterns.
        self.adaptive.record_feedback(message_id, "relevant", None)
        exact = self.adaptive._pattern("exact", self.adaptive.exact_signature(text))
        template = self.adaptive._pattern("template", self.adaptive.template_signature(text))
        self.assertIsNotNone(exact)
        self.assertIsNotNone(template)
        self.assertEqual(exact["relevant_votes"], 1)
        self.assertEqual(template["relevant_votes"], 1)
        self.assertEqual(self.adaptive.learned_relevance(text)["classification"], "relevant")


if __name__ == "__main__":
    unittest.main()
