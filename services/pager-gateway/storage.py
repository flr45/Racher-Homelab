from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_SETTINGS: dict[str, str] = {
    "gateway_name": "Racher Pager Gateway",
    "source_mode": "mock",
    "pdl_log_path": "/data/pdl.log",
    "pocsag_baud": "auto",
    "invert": "auto",
    "pushover_enabled": "0",
    "pushover_app_token": "",
    "pushover_user_key": "",
}


class Storage:
    def __init__(self, path: str) -> None:
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
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
                CREATE INDEX IF NOT EXISTS idx_messages_received_at
                    ON messages(received_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_station
                    ON messages(station);
                """
            )
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, value),
                )

            # Earlier MVP builds exposed station enable/disable switches. Pager
            # traffic without a station marker is valid, so those settings must
            # never become forwarding filters. Remove stale keys on upgrade.
            conn.execute(
                "DELETE FROM settings WHERE key IN (?, ?, ?, ?, ?)",
                (
                    "station_a_enabled",
                    "station_s_enabled",
                    "station_k_enabled",
                    "station_l_enabled",
                    "station_r_enabled",
                ),
            )

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def update_settings(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    def add_message(self, event: dict[str, Any]) -> int:
        received_at = event.get("received_at") or datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages(
                    received_at, protocol, baud, ric, function, station,
                    message, raw_line, source, notification_sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    received_at,
                    event.get("protocol", "POCSAG"),
                    event.get("baud"),
                    event.get("ric"),
                    event.get("function"),
                    event.get("station"),
                    event.get("message", ""),
                    event.get("raw_line", event.get("message", "")),
                    event.get("source", "unknown"),
                    1 if event.get("notification_sent") else 0,
                ),
            )
            return int(cur.lastrowid)

    def mark_notification_sent(self, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET notification_sent=1 WHERE id=?",
                (message_id,),
            )

    def list_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_message(self) -> dict[str, Any] | None:
        rows = self.list_messages(limit=1)
        return rows[0] if rows else None

    def message_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
        return int(row["count"])
