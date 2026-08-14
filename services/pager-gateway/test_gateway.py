import unittest

from gateway import detect_station, parse_pdl_line


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


if __name__ == "__main__":
    unittest.main()
