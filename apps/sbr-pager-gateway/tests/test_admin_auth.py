from __future__ import annotations

import os
import tempfile
import unittest


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = self.temp.name

        import admin_auth
        import storage

        self.admin_auth = admin_auth
        self.storage = storage
        storage.init_database()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_six_digit_pin_is_hashed_and_verified(self) -> None:
        self.admin_auth.set_admin_pin("123456")
        self.assertTrue(self.admin_auth.pin_is_configured())
        self.assertTrue(self.admin_auth.verify_admin_pin("123456"))
        self.assertFalse(self.admin_auth.verify_admin_pin("654321"))
        self.assertNotEqual(self.storage.get_setting("admin_pin_hash"), "123456")

    def test_invalid_pin_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.admin_auth.set_admin_pin("12345")
        with self.assertRaises(ValueError):
            self.admin_auth.set_admin_pin("abcdef")


if __name__ == "__main__":
    unittest.main()
