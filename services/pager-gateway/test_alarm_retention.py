from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from alarm_retention import _recent_rows, local_timezone


class _Storage:
    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class _Routing:
    def __init__(self, *, receive_all: bool = False, stations: list[str] | None = None) -> None:
        self.receive_all = receive_all
        self.stations = stations or []

    def user_receive_all(self, _user_id: int) -> bool:
        return self.receive_all

    def user_stations(self, _user_id: int) -> list[str]:
        return list(self.stations)


class AlarmRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "pager.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    baud INTEGER,
                    ric TEXT,
                    function TEXT,
                    station TEXT,
                    message TEXT NOT NULL,
                    raw_line TEXT NOT NULL,
                    source TEXT NOT NULL,
                    relevance_class TEXT NOT NULL DEFAULT 'unknown',
                    relevance_score REAL NOT NULL DEFAULT 1.0,
                    delivery_eligible INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE stations (
                    station_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO stations(station_key, name, active) VALUES
                    ('A', 'Slagelse', 1),
                    ('S', 'Sorø', 1);
                """
            )

        now_local = datetime.now(local_timezone())
        now_utc = datetime.now(timezone.utc)
        self._insert(
            (now_local - timedelta(days=8)).replace(tzinfo=None).isoformat(),
            "Slagelse",
            "for gammel",
            ric="8000001",
        )
        self._insert(
            (now_local - timedelta(days=6)).replace(tzinfo=None).isoformat(),
            "Slagelse",
            "slagelse recent",
            ric="6000001",
        )
        self._insert(
            (now_utc - timedelta(days=1)).isoformat(),
            "Sorø",
            "soroe recent",
            ric="1000001",
        )
        self._insert(
            (now_local - timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            "Slagelse",
            "suppressed",
            ric="0000001",
            delivery_eligible=0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _insert(
        self,
        received_at: str,
        station: str,
        message: str,
        *,
        ric: str,
        delivery_eligible: int = 1,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO messages(
                    received_at, protocol, baud, ric, function, station, message,
                    raw_line, source, relevance_class, relevance_score, delivery_eligible
                ) VALUES (?, 'POCSAG', 1200, ?, '1', ?, ?, ?, 'test', 'alarm', 1.0, ?)""",
                (received_at, ric, station, message, f"RAW {ric} {message}", delivery_eligible),
            )

    def _core(self, routing: _Routing):
        return SimpleNamespace(storage=_Storage(self.db_path), routing=routing)

    def test_admin_feed_contains_only_delivery_eligible_rows_from_last_seven_days(self):
        rows = _recent_rows(self._core(_Routing()))
        messages = [row["message"] for row in rows]
        self.assertIn("slagelse recent", messages)
        self.assertIn("soroe recent", messages)
        self.assertNotIn("for gammel", messages)
        self.assertNotIn("suppressed", messages)
        self.assertIn("ric", rows[0])

    def test_user_feed_keeps_station_routing_and_hides_decoder_metadata(self):
        rows = _recent_rows(self._core(_Routing(stations=["A"])), user_id=42)
        self.assertEqual([row["message"] for row in rows], ["slagelse recent"])
        self.assertNotIn("ric", rows[0])
        self.assertNotIn("raw_line", rows[0])
        self.assertNotIn("function", rows[0])

    def test_receive_all_user_gets_all_recent_alarm_stations_without_ric(self):
        rows = _recent_rows(self._core(_Routing(receive_all=True)), user_id=42)
        self.assertEqual({row["message"] for row in rows}, {"slagelse recent", "soroe recent"})
        self.assertTrue(all("ric" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
