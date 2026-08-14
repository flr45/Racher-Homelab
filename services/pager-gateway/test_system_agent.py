import configparser
import tempfile
import unittest
from pathlib import Path

from storage import Storage, validate_system_command
from system_agent import COMMANDS, _wifi_profile_name, sync_pdl_settings


class SystemAgentTests(unittest.TestCase):
    def test_fixed_commands_never_invoke_a_shell(self):
        self.assertEqual(
            set(COMMANDS),
            {"restart-pdl", "restart-gateway", "reboot", "restart-tunnel"},
        )
        for argv in COMMANDS.values():
            self.assertIsInstance(argv, list)
            self.assertTrue(argv)
            self.assertNotIn("sh", argv[0])
            self.assertNotIn("bash", argv[0])
            self.assertNotIn("-c", argv)

    def test_privileged_payload_validation(self):
        wifi = validate_system_command(
            "wifi-add", {"ssid": "Station WiFi", "password": "12345678"}
        )
        self.assertEqual(wifi["ssid"], "Station WiFi")
        self.assertEqual(wifi["password"], "12345678")
        self.assertRegex(_wifi_profile_name("Station WiFi"), r"^racher-wifi-[0-9a-f]{10}$")

        with self.assertRaises(ValueError):
            validate_system_command("wifi-add", {"ssid": "Station", "password": "kort"})
        with self.assertRaises(ValueError):
            validate_system_command("restore-backup", {"filename": "../../etc/passwd"})
        with self.assertRaises(ValueError):
            validate_system_command("wifi-remove", {"profile": "home-wifi"})
        with self.assertRaises(ValueError):
            validate_system_command("shell", {"command": "id"})

    def test_command_payload_is_not_returned_and_is_cleared_after_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "pager.db"))
            user_id = storage.create_user("admin", "Admin", "hash", "admin", None)
            command_id = storage.queue_system_command(
                "wifi-add",
                user_id,
                {"ssid": "Station WiFi", "password": "megethemmelig"},
            )
            listed = storage.list_system_commands()
            self.assertNotIn("payload", listed[0])
            self.assertNotIn("megethemmelig", str(listed))

            claimed = storage.claim_next_system_command()
            self.assertEqual(claimed["id"], command_id)
            self.assertEqual(claimed["payload"]["password"], "megethemmelig")
            storage.finish_system_command(command_id, True, "OK")

            with storage.connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM system_commands WHERE id=?", (command_id,)
                ).fetchone()
            self.assertEqual(row["payload"], "{}")

    def test_decoder_settings_are_applied_without_overwriting_hardware_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(str(root / "pager.db"))
            storage.update_settings({"pocsag_baud": "1200", "invert": "inverted"})

            config_path = root / "pdl" / "pdl.ini"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "[POCSAG]\n"
                "Enable=1\n"
                "Baud512=1\n"
                "Baud1200=1\n"
                "Baud2400=1\n\n"
                "[Audio]\n"
                "CaptureDevice=hw:9,0\n"
                "SampleRate=44100\n"
                "Config=1\n"
                "Enabled=1\n"
                "Invert=0\n\n"
                "[General]\n"
                "BlockDuplicate=0\n",
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
