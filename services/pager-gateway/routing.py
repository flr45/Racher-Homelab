from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STATIONS: tuple[dict[str, str], ...] = (
    {"key": "A", "name": "Slagelse"},
    {"key": "S", "name": "Sorø"},
    {"key": "K", "name": "Korsør"},
    {"key": "L", "name": "Skælskør"},
    {"key": "R", "name": "Ruds Vedby"},
)
STATION_BY_KEY = {item["key"]: item["name"] for item in STATIONS}
STATION_KEY_BY_NAME = {item["name"].casefold(): item["key"] for item in STATIONS}
ALL_STATION_KEYS = tuple(STATION_BY_KEY)


class RoutingStore:
    """RIC registry and user-to-station routing stored beside the gateway DB.

    Radio input is never filtered here. Every decoded event is stored first-class in
    the normal messages table; this store only classifies it and decides who should
    see/receive it.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ric_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ric TEXT NOT NULL UNIQUE,
                    station_key TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by INTEGER,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ric_codes_station ON ric_codes(station_key, active);

                CREATE TABLE IF NOT EXISTS user_station_subscriptions (
                    user_id INTEGER NOT NULL,
                    station_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, station_key),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_user_station_key
                    ON user_station_subscriptions(station_key, user_id);
                """
            )

    @staticmethod
    def normalize_ric(value: Any) -> str:
        ric = str(value or "").strip()
        if not ric.isdigit() or not 4 <= len(ric) <= 10:
            raise ValueError("RIC/capcode skal være 4-10 cifre")
        return ric

    @staticmethod
    def normalize_station_keys(values: Iterable[Any]) -> list[str]:
        keys: list[str] = []
        for raw in values:
            key = str(raw or "").strip().upper()
            if key not in STATION_BY_KEY:
                raise ValueError("Ukendt station")
            if key not in keys:
                keys.append(key)
        return keys

    def station_name(self, key: str) -> str | None:
        return STATION_BY_KEY.get(str(key or "").strip().upper())

    def station_key(self, name: str | None) -> str | None:
        return STATION_KEY_BY_NAME.get(str(name or "").strip().casefold())

    def classify(self, ric: str | None, fallback_station: str | None) -> tuple[str | None, str]:
        """Return (station name, source). Active RIC mapping has highest priority."""
        clean_ric = str(ric or "").strip()
        if clean_ric:
            with self._lock, self.connect() as conn:
                row = conn.execute(
                    "SELECT station_key FROM ric_codes WHERE ric=? AND active=1",
                    (clean_ric,),
                ).fetchone()
            if row:
                name = self.station_name(row["station_key"])
                if name:
                    return name, "ric"

        fallback_key = self.station_key(fallback_station)
        if fallback_key:
            return STATION_BY_KEY[fallback_key], "marker"
        return None, "unknown"

    def list_ric_codes(self) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT r.id, r.ric, r.station_key, r.label, r.active,
                          r.created_at, r.updated_at,
                          COUNT(m.id) AS message_count,
                          MAX(m.received_at) AS last_seen
                   FROM ric_codes r
                   LEFT JOIN messages m ON m.ric=r.ric
                   GROUP BY r.id
                   ORDER BY r.station_key, r.ric"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["station"] = self.station_name(item["station_key"])
            result.append(item)
        return result

    def get_ric_code(self, ric_id: int) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM ric_codes WHERE id=?", (ric_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["station"] = self.station_name(item["station_key"])
        return item

    def create_ric_code(self, ric: Any, station_key: Any, label: Any,
                        active: bool, created_by: int | None) -> int:
        clean_ric = self.normalize_ric(ric)
        key = self.normalize_station_keys([station_key])[0]
        clean_label = str(label or "").strip()[:120]
        now = self._now()
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO ric_codes(ric, station_key, label, active, created_at, updated_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (clean_ric, key, clean_label, 1 if active else 0, now, now, created_by),
            )
            if active:
                conn.execute(
                    "UPDATE messages SET station=? WHERE ric=?",
                    (STATION_BY_KEY[key], clean_ric),
                )
            return int(cur.lastrowid)

    def update_ric_code(self, ric_id: int, *, ric: Any | None = None,
                        station_key: Any | None = None, label: Any | None = None,
                        active: bool | None = None) -> dict[str, Any] | None:
        current = self.get_ric_code(ric_id)
        if not current:
            return None
        clean_ric = self.normalize_ric(current["ric"] if ric is None else ric)
        key = self.normalize_station_keys([
            current["station_key"] if station_key is None else station_key
        ])[0]
        clean_label = str(current["label"] if label is None else label or "").strip()[:120]
        enabled = bool(current["active"]) if active is None else bool(active)
        now = self._now()
        with self._lock, self.connect() as conn:
            conn.execute(
                """UPDATE ric_codes
                   SET ric=?, station_key=?, label=?, active=?, updated_at=?
                   WHERE id=?""",
                (clean_ric, key, clean_label, 1 if enabled else 0, now, ric_id),
            )
            if enabled:
                conn.execute(
                    "UPDATE messages SET station=? WHERE ric=?",
                    (STATION_BY_KEY[key], clean_ric),
                )
        return self.get_ric_code(ric_id)

    def delete_ric_code(self, ric_id: int) -> bool:
        with self._lock, self.connect() as conn:
            cur = conn.execute("DELETE FROM ric_codes WHERE id=?", (ric_id,))
            return cur.rowcount > 0

    def list_unknown_rics(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT m.ric, COUNT(*) AS message_count, MAX(m.received_at) AS last_seen,
                          (SELECT m2.message FROM messages m2
                           WHERE m2.ric=m.ric ORDER BY m2.id DESC LIMIT 1) AS sample_message
                   FROM messages m
                   WHERE m.ric IS NOT NULL AND TRIM(m.ric) != ''
                     AND NOT EXISTS (SELECT 1 FROM ric_codes r WHERE r.ric=m.ric)
                   GROUP BY m.ric
                   ORDER BY MAX(m.id) DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_user_stations(self, user_id: int, station_keys: Iterable[Any]) -> list[str]:
        keys = self.normalize_station_keys(station_keys)
        now = self._now()
        with self._lock, self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
            if not exists:
                raise ValueError("Brugeren findes ikke")
            conn.execute("DELETE FROM user_station_subscriptions WHERE user_id=?", (user_id,))
            conn.executemany(
                "INSERT INTO user_station_subscriptions(user_id, station_key, created_at) VALUES (?, ?, ?)",
                [(user_id, key, now) for key in keys],
            )
        return keys

    def user_stations(self, user_id: int) -> list[str]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT station_key FROM user_station_subscriptions WHERE user_id=? ORDER BY station_key",
                (user_id,),
            ).fetchall()
        return [str(row["station_key"]) for row in rows]

    def attach_user_stations(self, users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for user in users:
            keys = self.user_stations(int(user["id"]))
            user["stations"] = keys
            user["station_names"] = [STATION_BY_KEY[key] for key in keys if key in STATION_BY_KEY]
        return users

    def list_messages_for_user(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        keys = self.user_stations(user_id)
        names = [STATION_BY_KEY[key] for key in keys if key in STATION_BY_KEY]
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE station IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*names, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_push_subscriptions_for_event(self, station: str | None) -> list[dict[str, Any]]:
        key = self.station_key(station)
        with self._lock, self.connect() as conn:
            if key:
                rows = conn.execute(
                    """SELECT DISTINCT ps.*
                       FROM push_subscriptions ps
                       JOIN users u ON u.id=ps.user_id
                       JOIN user_station_subscriptions us ON us.user_id=u.id
                       WHERE u.active=1 AND us.station_key=?
                       ORDER BY ps.id""",
                    (key,),
                ).fetchall()
            else:
                # Unknown/unclassified traffic is an admin safety net only.
                rows = conn.execute(
                    """SELECT ps.* FROM push_subscriptions ps
                       JOIN users u ON u.id=ps.user_id
                       WHERE u.active=1 AND u.role='admin'
                       ORDER BY ps.id"""
                ).fetchall()
        return [dict(row) for row in rows]
