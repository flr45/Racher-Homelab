import unittest

import fsk_status_agent as fsk


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


if __name__ == "__main__":
    unittest.main()
