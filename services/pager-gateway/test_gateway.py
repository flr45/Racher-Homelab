import unittest

from gateway import detect_station, parse_pdl_line, public_message


class PagerParsingTests(unittest.TestCase):
    def test_known_station_marker_is_metadata(self):
        event = parse_pdl_line("(A) BRANDALARM 4200 Slagelse")
        self.assertIsNotNone(event)
        self.assertEqual(event.station, "Slagelse")
        self.assertEqual(event.message, "(A) BRANDALARM 4200 Slagelse")

    def test_island_message_without_station_marker_is_kept(self):
        text = "$8 ISL KA MØ M1 + V1 (1+5) Naturbrand-Mark, Høstet 4291 Ruds Vedby"
        event = parse_pdl_line(text)
        self.assertIsNotNone(event)
        self.assertIsNone(event.station)
        self.assertEqual(event.message, text)

    def test_oef_message_without_station_marker_is_kept(self):
        text = "@6 ØF HA(1+5)M/TS+V Haslev Campus BRANDALARM 4690 Haslev"
        event = parse_pdl_line(text)
        self.assertIsNotNone(event)
        self.assertIsNone(event.station)
        self.assertEqual(event.message, text)

    def test_vct_message_without_station_marker_is_kept(self):
        text = "VCT - ISL-Eftersyn Øvej 24 4340 Tølløse Udbrændte halmballer brænder stadigvæk lidt. 4230-8650"
        event = parse_pdl_line(text)
        self.assertIsNotNone(event)
        self.assertIsNone(event.station)
        self.assertEqual(event.message, text)

    def test_documented_pdw_pocsag_format_extracts_ric_message_and_timestamp(self):
        line = "1234567 14:13:33 17-08-09 POCSAG-1 ALPHA 1200 (A) Please call ASAP"
        event = parse_pdl_line(line)
        self.assertIsNotNone(event)
        self.assertEqual(event.ric, "1234567")
        self.assertEqual(event.function, "1")
        self.assertEqual(event.baud, 1200)
        self.assertEqual(event.message, "(A) Please call ASAP")
        self.assertEqual(event.station, "Slagelse")
        self.assertEqual(event.received_at, "2009-08-17T14:13:33")
        self.assertEqual(event.raw_line, line)
        self.assertNotIn("1234567", event.message)

    def test_pdw_four_digit_year_timestamp_is_preserved(self):
        line = "7654321 01:02:03 14-08-2026 POCSAG ALPHA 2400 test"
        event = parse_pdl_line(line)
        self.assertIsNotNone(event)
        self.assertEqual(event.received_at, "2026-08-14T01:02:03")

    def test_labeled_address_is_accepted_as_ric_but_not_public_text(self):
        event = parse_pdl_line("Address: 7654321 POCSAG 512 MESSAGE: test")
        self.assertIsNotNone(event)
        self.assertEqual(event.ric, "7654321")
        self.assertEqual(event.baud, 512)
        self.assertEqual(event.message, "test")
        self.assertNotIn("7654321", event.message)

    def test_public_message_strips_labeled_ric_metadata(self):
        text = public_message("RIC: 1234567 MESSAGE: BRANDALARM Testvej 1")
        self.assertEqual(text, "BRANDALARM Testvej 1")
        self.assertNotIn("1234567", text)


if __name__ == "__main__":
    unittest.main()
