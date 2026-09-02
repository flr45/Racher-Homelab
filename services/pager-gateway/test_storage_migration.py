from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from storage import Storage


class StorageMigrationTests(unittest.TestCase):
    def test_legacy_messages_table_migrates_before_new_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pager.db")
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        received_at TEXT NOT NULL,
                        protocol TEXT NOT NULL DEFAULT 'POCSAG',
                        baud INTEGER,
                        ric TEXT,
                        function TEXT,
                        station TEXT,
                        message TEXT NOT NULL,
                        raw_line TEXT NOT NULL,
                        source TEXT NOT NULL,
                        notification_sent INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO messages(
                        received_at, protocol, ric, station, message, raw_line, source
                    ) VALUES (
                        '2026-08-14T20:00:00+00:00', 'POCSAG', '1234567', 'Slagelse',
                        'Gammel testmelding', 'legacy raw', 'legacy-test'
                    );
                    """
                )

            storage = Storage(db_path)

            with storage.connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
                indexes = {row["name"] for row in conn.execute("PRAGMA index_list(messages)").fetchall()}
                row = conn.execute("SELECT * FROM messages WHERE id=1").fetchone()

            for column in (
                "message_fingerprint", "relevance_class", "relevance_score",
                "suppressed_reason", "duplicate_of", "delivery_eligible", "decision_reason",
            ):
                self.assertIn(column, columns)
            self.assertIn("idx_messages_fingerprint", indexes)
            self.assertIn("idx_messages_delivery", indexes)
            self.assertEqual(row["message"], "Gammel testmelding")
            self.assertEqual(row["relevance_class"], "unknown")
            self.assertEqual(row["delivery_eligible"], 1)


if __name__ == "__main__":
    unittest.main()
