import hashlib
import unittest

from werkzeug.security import check_password_hash, generate_password_hash

import sitecustomize


class PasswordCompatibilityTests(unittest.TestCase):
    def test_fallback_matches_native_scrypt_when_available(self):
        if not hasattr(hashlib, "scrypt"):
            self.skipTest("runtime has no native hashlib.scrypt")

        password = b"meget-hemmelig-testkode"
        salt = b"0123456789abcdef"
        kwargs = {"salt": salt, "n": 2**14, "r": 8, "p": 1, "dklen": 64}

        native = hashlib.scrypt(password, **kwargs)
        fallback = sitecustomize._cryptography_scrypt(password, **kwargs)
        self.assertEqual(native, fallback)

    def test_werkzeug_hashing_works_without_native_hashlib_scrypt(self):
        original = getattr(hashlib, "scrypt", None)
        if hasattr(hashlib, "scrypt"):
            delattr(hashlib, "scrypt")

        try:
            installed = sitecustomize.install_scrypt_fallback()
            self.assertTrue(installed)
            password_hash = generate_password_hash("meget-hemmelig-testkode")
            self.assertTrue(password_hash.startswith("scrypt:"))
            self.assertTrue(check_password_hash(password_hash, "meget-hemmelig-testkode"))
            self.assertFalse(check_password_hash(password_hash, "forkert-kode"))
        finally:
            if original is None:
                try:
                    delattr(hashlib, "scrypt")
                except AttributeError:
                    pass
            else:
                setattr(hashlib, "scrypt", original)


if __name__ == "__main__":
    unittest.main()
