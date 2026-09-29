from __future__ import annotations

import unittest

from modem_reader import parse_registration, parse_signal_quality


class ModemNetworkParsingTests(unittest.TestCase):
    def test_home_registration_is_online(self):
        registered, state = parse_registration("\r\n+CREG: 0,1\r\n\r\nOK\r\n")
        self.assertTrue(registered)
        self.assertEqual(state, "registered-home")

    def test_roaming_registration_is_online(self):
        registered, state = parse_registration("\r\n+CREG: 0,5\r\n\r\nOK\r\n")
        self.assertTrue(registered)
        self.assertEqual(state, "registered-roaming")

    def test_searching_and_denied_are_not_registered(self):
        self.assertEqual(
            parse_registration("\r\n+CREG: 0,2\r\n"),
            (False, "searching"),
        )
        self.assertEqual(
            parse_registration("\r\n+CREG: 0,3\r\n"),
            (False, "denied"),
        )

    def test_signal_quality_has_dbm_and_unknown_99(self):
        self.assertEqual(
            parse_signal_quality("\r\n+CSQ: 20,99\r\n"),
            {"rssi": 20, "ber": 99, "dbm": -73},
        )
        self.assertEqual(
            parse_signal_quality("\r\n+CSQ: 99,99\r\n"),
            {"rssi": 99, "ber": 99, "dbm": None},
        )


if __name__ == "__main__":
    unittest.main()
