from __future__ import annotations

import unittest

from burst_consensus import consensus_message, same_nr_burst


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


if __name__ == "__main__":
    unittest.main()
