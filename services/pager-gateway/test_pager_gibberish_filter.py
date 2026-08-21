from __future__ import annotations

import unittest
from types import SimpleNamespace

from pager_gibberish_filter import decoder_gibberish_reason, install_gibberish_filter


class PagerGibberishFilterTests(unittest.TestCase):
    def test_observed_long_decoder_garbage_is_suppressed(self):
        message = "??hb``@$ÅOgiKl · &WkeARMAEAPkgADewÅIKeAZ@L_eÆA@jWekiIeewÅIKe?"
        self.assertEqual(decoder_gibberish_reason(message), "decoder-gibberish")

    def test_real_ringsted_dispatch_is_not_suppressed(self):
        message = (
            "@5 NR RI(1+5)M+V · Bygn.brand-Villa/Rækkehus · 4100 Ringsted · "
            "Skur ifm hus brænDgr - form. ukrudtbrænder"
        )
        self.assertIsNone(decoder_gibberish_reason(message))

    def test_normal_free_text_without_alarm_keywords_is_not_suppressed(self):
        self.assertIsNone(
            decoder_gibberish_reason(
                "Teknisk melding fra vagtcentralen om ændret adresse og adgangsvej"
            )
        )

    def test_wrapper_marks_live_pdl_only(self):
        seen = []

        def original(event):
            seen.append(event.decoder_noise_reason)
            return 42

        core = SimpleNamespace(ingest_event=original)
        install_gibberish_filter(core)

        event = SimpleNamespace(
            source="pdl-file",
            message="??hb``@$ÅOgiKl · &WkeARMAEAPkgADewÅIKeAZ@L_eÆA@jWekiIeewÅIKe?",
            decoder_noise_reason=None,
        )
        self.assertEqual(core.ingest_event(event), 42)
        self.assertEqual(event.decoder_noise_reason, "decoder-gibberish")
        self.assertEqual(seen, ["decoder-gibberish"])

        mock_event = SimpleNamespace(
            source="mock",
            message="??hb``@$ÅOgiKl · &WkeARMAEAPkgADewÅIKeAZ@L_eÆA@jWekiIeewÅIKe?",
            decoder_noise_reason=None,
        )
        core.ingest_event(mock_event)
        self.assertIsNone(mock_event.decoder_noise_reason)


if __name__ == "__main__":
    unittest.main()
