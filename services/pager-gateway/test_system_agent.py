import configparser
import tempfile
import unittest
from pathlib import Path

from storage import Storage
from system_agent import COMMANDS, sync_pdl_settings


class SystemAgentTests(unittest.TestCase):
    def test_only_expected_actions_are_executable(self):
        self.assertEqual(set(COMMANDS), {"restart-pdl", "restart-gateway", "reboot"})
        for argv in COMMANDS.values():
            self.assertIsInstance(argv, list)
            self.assertTrue(argv)
            self.assertNotIn("sh", argv[0])
            self.assertNotIn("bash", argv[0])

    def test_decoder_settings_are_applied_without_overwriting_hardware_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(str(root / "pager.db"))
            storage.update_settings({"pocsag_baud": "1200", "invert": "inverted"})

            config_path = root / "pdl" / "pdl.ini"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """[POCSAG]\nEnable=1\nBaud512=1\nBaud1200=1\nBaud2400=1\n\n"
                "[Audio]\nCaptureDevice=hw:9,0\nSampleRate=44100\nConfig=1\nEnabled=1\nInvert=0\n\n"
                "[General]\nBlockDuplicate=0\n""",
                encoding="utf-8",
            )

            applied = sync_pdl_settings(storage, config_path)
            self.assertEqual(applied, {"pocsag_baud": "1200", "invert": "inverted"})

            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(config_path, encoding="utf-8")

            self.assertEqual(parser.get("POCSAG", "Baud512"), "0")
            self.assertEqual(parser.get("POCSAG", "Baud1200"), "1")
            self.assertEqual(parser.get("POCSAG", "Baud2400"), "0")
            self.assertEqual(parser.get("Audio", "Invert"), "1")
            self.assertEqual(parser.get("Audio", "CaptureDevice"), "hw:9,0")
            self.assertEqual(parser.get("Audio", "SampleRate"), "44100")
            self.assertEqual(parser.get("General", "BlockDuplicate"), "0")

    def test_auto_baud_enables_all_pocsag_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(str(root / "pager.db"))
            storage.update_settings({"pocsag_baud": "auto", "invert": "normal"})
            config_path = root / "pdl.ini"

            sync_pdl_settings(storage, config_path)

            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(config_path, encoding="utf-8")
            self.assertEqual(parser.get("POCSAG", "Baud512"), "1")
            self.assertEqual(parser.get("POCSAG", "Baud1200"), "1")
            self.assertEqual(parser.get("POCSAG", "Baud2400"), "1")
            self.assertEqual(parser.get("Audio", "Invert"), "0")
            self.assertEqual(parser.get("Audio", "CaptureDevice"), "default")
            self.assertEqual(parser.get("Audio", "SampleRate"), "48000")


if __name__ == "__main__":
    unittest.main()
