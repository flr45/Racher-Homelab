import hashlib
import unittest

from werkzeug.security import check_password_hash

from app import hash_password


class PasswordCompatibilityTests(unittest.TestCase):
    def test_password_hashing_does_not_require_hashlib_scrypt(self):
        original = getattr(hashlib, "scrypt", None)
        had_scrypt = hasattr(hashlib, "scrypt")
        if had_scrypt:
            delattr(hashlib, "scrypt")

        try:
            password_hash = hash_password("meget-hemmelig-testkode")
            self.assertTrue(password_hash.startswith("pbkdf2:sha256:600000$"))
            self.assertTrue(check_password_hash(password_hash, "meget-hemmelig-testkode"))
            self.assertFalse(check_password_hash(password_hash, "forkert-kode"))
        finally:
            if had_scrypt:
                setattr(hashlib, "scrypt", original)


if __name__ == "__main__":
    unittest.main()
