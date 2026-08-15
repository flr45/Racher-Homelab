import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fsk_status_agent as fsk
from storage import Storage


class FskStatusTests(unittest.TestCase):
    def test_parse_udev_properties(self):
        values = fsk.parse_udev_properties(
            "ID_VENDOR=FTDI\nID_MODEL=FT232R_USB_UART\nID_SERIAL_SHORT=A12345\n"
        )
        self.assertEqual(values["ID_VENDOR"], "FTDI")
        self.assertEqual(values["ID_MODEL"], "FT232R_USB_UART")
        self.assertEqual(values["ID_SERIAL_SHORT"], "A12345")

    def test_choose_device_prefers_ftdi_score(self):
        candidates = [
            {"path": "/dev/ttyACM0", "real": "/dev/ttyACM0", "props": {}, "score": 10},
            {"path": "/dev/serial/by-id/usb-FTDI_FT232R", "real": "/dev/ttyUSB0", "props": {}, "score": 160},
        ]
        selected = fsk.choose_device(candidates, explicit="")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["real"], "/dev/ttyUSB0")

    def test_choose_device_honours_explicit_path(self):
        candidates = [
            {"path": "/dev/ttyUSB0", "real": "/dev/ttyUSB0", "props": {}, "score": 120},
            {"path": "/dev/ttyACM0", "real": "/dev/ttyACM0", "props": {}, "score": 10},
        ]
        selected = fsk.choose_device(candidates, explicit="/dev/ttyACM0")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["path"], "/dev/ttyACM0")

    def test_choose_device_empty(self):
        self.assertIsNone(fsk.choose_device([], explicit=""))

    def test_once_seen_hardware_remains_commissioned_when_unplugged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "pager.db")
            connected = {
                "fsk_usb_connected": "1",
                "fsk_usb_pdl_in_use": "1",
                "fsk_usb_last_seen": "2026-08-15T10:00:00+00:00",
            }
            missing = {
                "fsk_usb_connected": "0",
                "fsk_usb_pdl_in_use": "0",
                "fsk_usb_last_seen": "",
            }

            with patch.object(fsk, "DB_PATH", db_path), patch.object(fsk, "collect_status", return_value=connected):
                self.assertEqual(fsk.main(), 0)
            with patch.object(fsk, "DB_PATH", db_path), patch.object(fsk, "collect_status", return_value=missing):
                self.assertEqual(fsk.main(), 0)

            runtime = Storage(db_path).get_runtime_status()
            self.assertEqual(runtime["fsk_usb_ever_seen"]["value"], "1")
            self.assertEqual(runtime["fsk_usb_connected"]["value"], "0")
            self.assertEqual(runtime["fsk_usb_last_seen"]["value"], "2026-08-15T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
