from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from pushover_destinations import PushoverDestinationStore, mask_key


class PushoverDestinationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "pager.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    created_by INTEGER,
                    last_login_at TEXT
                )
                """
            )
        self.store = PushoverDestinationStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mask_key_keeps_only_small_identifying_edges(self):
        key = "u123456789012345678901234567890"
        masked = mask_key(key)
        self.assertTrue(masked.startswith("u123"))
        self.assertTrue(masked.endswith(key[-6:]))
        self.assertNotIn(key[4:-6], masked)

    def test_add_and_list_never_exposes_secret_key(self):
        key = "u123456789012345678901234567890"
        created = self.store.add("Frederik", key)
        self.assertEqual(created["label"], "Frederik")
        self.assertTrue(created["active"])
        self.assertNotIn("user_key", created)
        self.assertNotEqual(created["key_masked"], key)

        rows = self.store.list_all()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("user_key", rows[0])
        self.assertEqual(rows[0]["key_masked"], created["key_masked"])

        secret_rows = self.store.list_active_secret()
        self.assertEqual(secret_rows[0]["user_key"], key)

    def test_toggle_and_delete(self):
        created = self.store.add("Vagtleder", "u123456789012345678901234567890")
        updated = self.store.set_active(created["id"], False)
        self.assertIsNotNone(updated)
        self.assertFalse(updated["active"])
        self.assertEqual(self.store.list_active_secret(), [])
        self.assertTrue(self.store.delete(created["id"]))
        self.assertEqual(self.store.list_all(), [])

    def test_duplicate_and_invalid_keys_are_rejected(self):
        key = "u123456789012345678901234567890"
        self.store.add("Primær", key)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add("Dublet", key)
        with self.assertRaises(ValueError):
            self.store.add("Forkert", "kort")
        with self.assertRaises(ValueError):
            self.store.add("Forkert", "not-a-valid-key-with-dashes")


if __name__ == "__main__":
    unittest.main()
