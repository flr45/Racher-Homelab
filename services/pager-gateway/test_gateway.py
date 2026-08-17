import os
import tempfile
import unittest
from pathlib import Path

from gateway import (
    FileTailSource,
    decode_pocsag_danish_charset,
    detect_station,
    parse_pdl_line,
    public_message,
)


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
        self.assertIsNone(event.decoder_noise_reason)

    def test_pdw_four_digit_year_timestamp_is_preserved(self):
        line = "7654321 01:02:03 14-08-2026 POCSAG ALPHA 2400 test"
        event = parse_pdl_line(line)
        self.assertIsNotNone(event)
        self.assertEqual(event.received_at, "2026-08-14T01:02:03")

    def test_danish_iso646_character_table(self):
        self.assertEqual(decode_pocsag_danish_charset("[\\]{|}"), "ÆØÅæøå")

    def test_live_pdl_translates_danish_pager_characters(self):
        line = "0001191 12:00:34 17-08-2026 POCSAG-4 ALPHA 1200 DAGENS PR\\VE TIL ISL"
        event = parse_pdl_line(line, source="pdl-file")
        self.assertIsNotNone(event)
        self.assertEqual(event.message, "DAGENS PRØVE TIL ISL")
        self.assertEqual(event.raw_line, line)

    def test_numeric_pdl_payload_is_not_danish_translated_and_is_marked_noise(self):
        line = "0001191 12:00:34 17-08-2026 POCSAG-4 NUMERIC 1200 40]04"
        event = parse_pdl_line(line, source="pdl-file")
        self.assertIsNotNone(event)
        self.assertEqual(event.message, "40]04")
        self.assertNotIn("Å", event.message)
        self.assertEqual(event.decoder_noise_reason, "decoder-non-alpha")

    def test_bare_decoder_code_is_marked_noise_without_losing_raw_line(self):
        line = "40*04"
        event = parse_pdl_line(line, source="pdl-file")
        self.assertIsNotNone(event)
        self.assertEqual(event.raw_line, line)
        self.assertEqual(event.message, line)
        self.assertEqual(event.decoder_noise_reason, "decoder-code")

    def test_short_lowercase_suffix_is_marked_fragment(self):
        event = parse_pdl_line("førerhus, spredt sig", source="pdl-file")
        self.assertIsNotNone(event)
        self.assertEqual(event.decoder_noise_reason, "decoder-fragment")

    def test_repeated_question_mark_field_separators_are_cleaned(self):
        text = "$6 ISL KA, KB V1 (0+2)??Naturbrand-Mark, Høstet??4450 Jyderup??Traktor holder på mark"
        cleaned = public_message(text)
        self.assertEqual(
            cleaned,
            "$6 ISL KA, KB V1 (0+2) · Naturbrand-Mark, Høstet · 4450 Jyderup · Traktor holder på mark",
        )
        self.assertNotIn("??", cleaned)

    def test_single_question_mark_is_preserved_as_possible_decode_error(self):
        self.assertEqual(public_message("BRANDALARM H?stet"), "BRANDALARM H?stet")

    def test_mock_source_does_not_translate_ascii_punctuation(self):
        text = r"MOCK [test] path\file {x|y}"
        event = parse_pdl_line(text, source="mock")
        self.assertIsNotNone(event)
        self.assertEqual(event.message, text)
        self.assertIsNone(event.decoder_noise_reason)

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

    def test_file_tail_detects_replaced_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pdl.log"
            path.write_text("old\n", encoding="utf-8")
            with path.open("r", encoding="utf-8") as handle:
                self.assertTrue(FileTailSource._same_file(path, handle))
                replacement = Path(tmp) / "pdl.log.new"
                replacement.write_text("new\n", encoding="utf-8")
                os.replace(replacement, path)
                self.assertFalse(FileTailSource._same_file(path, handle))

    def test_file_tail_first_start_skips_old_backlog_but_restart_resumes_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pdl.log"
            path.write_text("historic\n", encoding="utf-8")
            source = FileTailSource(lambda: str(path), lambda _: None)

            with path.open("r", encoding="utf-8") as handle:
                position = source._resume_position(path, handle)
                self.assertEqual(position, path.stat().st_size)

            with path.open("a", encoding="utf-8") as writer:
                writer.write("live-one\n")
                writer.flush()
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(len("historic\n"))
                self.assertEqual(handle.readline(), "live-one\n")
                source._save_cursor(path, handle)

            with path.open("a", encoding="utf-8") as writer:
                writer.write("during-restart\n")
                writer.flush()

            restarted = FileTailSource(lambda: str(path), lambda _: None)
            with path.open("r", encoding="utf-8") as handle:
                restarted._resume_position(path, handle)
                self.assertEqual(handle.readline(), "during-restart\n")

    def test_file_tail_new_inode_or_truncation_restarts_at_beginning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pdl.log"
            path.write_text("one\n", encoding="utf-8")
            source = FileTailSource(lambda: str(path), lambda _: None)
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(0, os.SEEK_END)
                source._save_cursor(path, handle)

            replacement = Path(tmp) / "replacement.log"
            replacement.write_text("new-inode\n", encoding="utf-8")
            os.replace(replacement, path)
            with path.open("r", encoding="utf-8") as handle:
                source._resume_position(path, handle)
                self.assertEqual(handle.tell(), 0)
                self.assertEqual(handle.readline(), "new-inode\n")

            with path.open("r", encoding="utf-8") as handle:
                handle.seek(0, os.SEEK_END)
                source._save_cursor(path, handle)
            path.write_text("short\n", encoding="utf-8")
            with path.open("r", encoding="utf-8") as handle:
                source._resume_position(path, handle)
                self.assertEqual(handle.tell(), 0)
                self.assertEqual(handle.readline(), "short\n")


if __name__ == "__main__":
    unittest.main()
