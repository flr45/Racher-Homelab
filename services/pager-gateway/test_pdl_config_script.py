import configparser
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PdlConfigureScriptTests(unittest.TestCase):
    def test_database_decoder_settings_override_stale_env_defaults(self):
        service_dir = Path(__file__).resolve().parent
        script = service_dir / "pdl" / "configure-pdl.sh"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            database = state / "pager.db"
            database.touch()

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_sqlite = fake_bin / "sqlite3"
            fake_sqlite.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *pocsag_baud*) printf '1200\\n' ;;\n"
                "  *invert*) printf 'inverted\\n' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_sqlite.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                "PAGER_STATE_ROOT": str(state),
                "PAGER_DB_PATH": str(database),
                "PDL_BAUD_512": "1",
                "PDL_BAUD_1200": "1",
                "PDL_BAUD_2400": "1",
                "PDL_INVERT": "0",
                "PDL_INPUT_MODE": "fsk-usb",
            })

            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(state / "pdl" / "pdl.ini", encoding="utf-8")
            self.assertEqual(parser.get("POCSAG", "Baud512"), "0")
            self.assertEqual(parser.get("POCSAG", "Baud1200"), "1")
            self.assertEqual(parser.get("POCSAG", "Baud2400"), "0")
            self.assertEqual(parser.get("POCSAG", "ShowBoth"), "1")
            self.assertEqual(parser.get("Audio", "Invert"), "1")
            self.assertEqual(parser.get("RS232", "DecodeMode"), "1")
            self.assertEqual(parser.get("General", "ShowTone"), "0")
            self.assertEqual(parser.get("General", "ShowNumeric"), "1")
            self.assertEqual(parser.get("General", "ShowMisc"), "0")

    def test_combined_1200_2400_mode_disables_512(self):
        service_dir = Path(__file__).resolve().parent
        script = service_dir / "pdl" / "configure-pdl.sh"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            database = state / "pager.db"
            database.touch()

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_sqlite = fake_bin / "sqlite3"
            fake_sqlite.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *pocsag_baud*) printf '1200+2400\\n' ;;\n"
                "  *invert*) printf 'normal\\n' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_sqlite.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                "PAGER_STATE_ROOT": str(state),
                "PAGER_DB_PATH": str(database),
                "PDL_BAUD_512": "1",
                "PDL_BAUD_1200": "0",
                "PDL_BAUD_2400": "0",
                "PDL_INPUT_MODE": "fsk-usb",
            })

            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(state / "pdl" / "pdl.ini", encoding="utf-8")
            self.assertEqual(parser.get("POCSAG", "Baud512"), "0")
            self.assertEqual(parser.get("POCSAG", "Baud1200"), "1")
            self.assertEqual(parser.get("POCSAG", "Baud2400"), "1")
            self.assertEqual(parser.get("Audio", "Invert"), "0")
            self.assertEqual(parser.get("RS232", "DecodeMode"), "1")


if __name__ == "__main__":
    unittest.main()
