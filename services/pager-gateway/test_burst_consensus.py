from __future__ import annotations

import unittest

from burst_consensus import consensus_message, consensus_quality, same_nr_burst


class BurstConsensusTests(unittest.TestCase):
    def setUp(self):
        # These are the public-text equivalents of the real PDL copies observed
        # on 20-08-2026 at 12:35 after Danish ISO-646 translation.
        self.copies = [
            "@8 NR RI(1+5)M+S · Ringsted(?vømmeland · BRANDALARM · 4100 Ringsted",
            "@8 NR RM*1+5)M+S · R`ngsted Svømmeland · BRANDALARM · 4100 Ringsted",
            "@8 NR RI(1+5)M+S · Ringsted Svlmv2v",
            "@8 NR RI(1+5)M+S · Ringstul Svømmeland · BRANDALARM · 410x Zingsted",
        ]
        self.expected = (
            "@8 NR RI(1+5)M+S · Ringsted Svømmeland · BRANDALARM · 4100 Ringsted"
        )

    def test_observed_first_three_copies_reconstruct_working_reference(self):
        self.assertEqual(consensus_message(self.copies[:3]), self.expected)

    def test_observed_four_copies_reconstruct_working_reference(self):
        self.assertEqual(consensus_message(self.copies), self.expected)

    def test_observed_full_corrupt_copies_belong_to_same_burst(self):
        self.assertTrue(same_nr_burst(self.copies[0], self.copies[1]))
        self.assertTrue(same_nr_burst(self.copies[0], self.copies[3]))

    def test_observed_short_copy_can_join_clean_dispatch_key(self):
        self.assertTrue(same_nr_burst(self.copies[0], self.copies[2]))

    def test_two_different_full_ringsted_dispatches_are_not_merged_by_city_alone(self):
        other = (
            "@8 NR RI(1+5)M+S · Ringsted Station · AUTOMATISK BRANDALARM · 4100 Ringsted"
        )
        self.assertFalse(same_nr_burst(self.copies[0], other))

    def test_non_nr_message_is_not_a_burst_candidate(self):
        self.assertFalse(
            same_nr_burst(
                self.copies[0],
                "$9 ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic",
            )
        )

    def test_single_copy_can_never_claim_high_decode_confidence(self):
        quality = consensus_quality([self.expected])
        self.assertEqual(quality["copy_count"], 1)
        self.assertEqual(quality["label"], "low")
        self.assertLess(quality["confidence"], 0.50)

    def test_three_observed_copies_wait_for_more_evidence(self):
        quality = consensus_quality(self.copies[:3])
        self.assertEqual(quality["copy_count"], 3)
        self.assertEqual(quality["label"], "medium")
        self.assertLess(quality["confidence"], 0.82)

    def test_four_observed_copies_reach_high_decode_confidence(self):
        quality = consensus_quality(self.copies)
        self.assertEqual(quality["copy_count"], 4)
        self.assertEqual(quality["label"], "high")
        self.assertGreaterEqual(quality["confidence"], 0.85)

    def test_three_clean_copies_can_flush_early(self):
        quality = consensus_quality([self.expected, self.expected, self.expected])
        self.assertEqual(quality["copy_count"], 3)
        self.assertGreaterEqual(quality["confidence"], 0.82)


if __name__ == "__main__":
    unittest.main()
