from __future__ import annotations

import unittest

from alarm_rules import _quality_noise_reason
from burst_consensus import same_nr_burst


class NaestvedDispatchQualityTests(unittest.TestCase):
    def setUp(self):
        self.clean = (
            "@0 MN NÆ(1+5)M+S · Parkeringshuset · BRANDALARM · 4700 Næstved"
        )
        self.damaged_short = "@0 MN NÆ(1+5)M+S · Park%y4gs9"

    def test_observed_naestved_copies_belong_to_same_burst(self):
        self.assertTrue(same_nr_burst(self.clean, self.damaged_short))
        self.assertTrue(same_nr_burst(self.damaged_short, self.clean))

    def test_observed_short_naestved_copy_is_decoder_partial(self):
        self.assertEqual(_quality_noise_reason(self.damaged_short), "decoder-partial")

    def test_complete_naestved_alarm_remains_deliverable_quality(self):
        self.assertIsNone(_quality_noise_reason(self.clean))

    def test_different_complete_naestved_alarm_is_not_merged(self):
        other = (
            "@0 MN NÆ(1+5)M+S · Næstved Station · BRANDALARM · 4700 Næstved"
        )
        self.assertFalse(same_nr_burst(self.clean, other))


if __name__ == "__main__":
    unittest.main()
