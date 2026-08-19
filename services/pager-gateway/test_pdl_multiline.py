import tempfile
import time
import unittest
from pathlib import Path

from gateway import FileTailSource, parse_pdl_line
from pdl_multiline import install_pdl_multiline_tail, join_wrapped_pdl_lines


class PdlMultilineTests(unittest.TestCase):
    def wait_for_source_running(self, source: FileTailSource, timeout: float = 1.5) -> None:
        deadline = time.monotonic() + timeout
        while source.status.get("state") != "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(source.status.get("state"), "running", source.status)

    def test_real_wrapped_kalundborg_message_is_joined(self):
        lines = [
            " 0000999   14:17:56 19-08-26 POCSAG-4  ALPHA   1200   ISL KA, KB M1 (1+3) -??Bygn.brand-Mindre brand??Stenagervej 6??4400 Kalundborg??Ulovlig afbr{nding i halmfyr, ryger ud af\n",
            "                                                     l}gen. Sort r|g\n",
        ]
        logical = join_wrapped_pdl_lines(lines)
        event = parse_pdl_line(logical, source="pdl-file")

        self.assertIsNotNone(event)
        self.assertEqual(event.ric, "0000999")
        self.assertEqual(event.baud, 1200)
        self.assertIn("Ulovlig afbrænding i halmfyr, ryger ud af lågen. Sort røg", event.message)
        self.assertNotIn("\n", logical)

    def test_live_tail_emits_wrapped_alpha_as_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pdl.log"
            path.write_text("", encoding="utf-8")
            seen = []
            source = FileTailSource(lambda: str(path), seen.append)
            install_pdl_multiline_tail(source, flush_delay_seconds=0.05)
            source.start()
            try:
                self.wait_for_source_running(source)
                with path.open("a", encoding="utf-8") as writer:
                    writer.write(
                        " 0000999   14:17:56 19-08-26 POCSAG-4  ALPHA   1200   "
                        "ISL KA, KB M1 (1+3) -??Stenagervej 6??Ulovlig afbr{nding i halmfyr, ryger ud af\n"
                    )
                    writer.write("                                                     l}gen. Sort r|g\n")
                    writer.flush()

                deadline = time.monotonic() + 1.0
                while len(seen) < 1 and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                source.stop()

            self.assertEqual(len(seen), 1)
            event = parse_pdl_line(seen[0], source="pdl-file")
            self.assertIsNotNone(event)
            self.assertEqual(event.ric, "0000999")
            self.assertIn("ryger ud af lågen. Sort røg", event.message)

    def test_next_decoder_header_flushes_pending_alpha_before_new_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pdl.log"
            path.write_text("", encoding="utf-8")
            seen = []
            source = FileTailSource(lambda: str(path), seen.append)
            install_pdl_multiline_tail(source, flush_delay_seconds=0.5)
            source.start()
            try:
                self.wait_for_source_running(source)
                with path.open("a", encoding="utf-8") as writer:
                    writer.write(" 0000999 14:17:56 19-08-26 POCSAG-4 ALPHA 1200 Første del\n")
                    writer.write("                                                     anden del\n")
                    writer.write(" 0174760 14:19:48 19-08-26 POCSAG-1 NUMERIC 1200 00804\n")
                    writer.flush()

                deadline = time.monotonic() + 1.0
                while len(seen) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                source.stop()

            self.assertEqual(len(seen), 2)
            self.assertIn("Første del anden del", seen[0])
            self.assertIn("0174760", seen[1])


if __name__ == "__main__":
    unittest.main()
