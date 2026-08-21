from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_STATIONS: tuple[dict[str, str], ...] = (
    {"key": "A", "name": "Slagelse", "marker": "A"},
    {"key": "S", "name": "Sorø", "marker": "S"},
    {"key": "K", "name": "Korsør", "marker": "K"},
    {"key": "L", "name": "Skælskør", "marker": "L"},
    {"key": "R", "name": "Ruds Vedby", "marker": "R"},
)
STATION_NAME_RE = re.compile(r"^[A-Za-zÆØÅæøå0-9][A-Za-zÆØÅæøå0-9 .&'()/_-]{1,79}$")
EXPLICIT_STATION_PATTERNS = (
    re.compile(r"\b(?:BRANDSTATION|STATION)\s+([A-ZÆØÅ][A-Za-zÆØÅæøå-]{2,}(?:\s+[A-ZÆØÅ][A-Za-zÆØÅæøå-]{2,}){0,2})\b", re.I),
    re.compile(r"\b([A-ZÆØÅ][A-Za-zÆØÅæøå-]{2,}(?:\s+[A-ZÆØÅ][A-Za-zÆØÅæøå-]{2,}){0,2})\s+(?:BRANDVÆSEN|BEREDSKAB|BRAND\s*(?:&|OG)\s*REDNING)\b", re.I),
)


class RoutingStore:
    """Dynamic RIC/area routing with conservative local station discovery.

    Every decoded radio event is still stored. This class only classifies delivery
    areas and chooses recipients. New areas may be learned from repeated explicit
    station/brigade wording, never merely from an incident postal locality.
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
                CREATE TABLE IF NOT EXISTS stations (
                    station_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    marker TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    auto_created INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source TEXT NOT NULL DEFAULT 'admin',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ric_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ric TEXT NOT NULL UNIQUE,
                    station_key TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by INTEGER,
                    auto_created INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 1.0,
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

                CREATE TABLE IF NOT EXISTS user_routing_preferences (
                    user_id INTEGER PRIMARY KEY,
                    receive_all INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS station_discovery_evidence (
                    candidate_name TEXT NOT NULL COLLATE NOCASE,
                    ric TEXT NOT NULL DEFAULT '',
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    sample_message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(candidate_name, ric)
                );
                """
            )

            ric_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ric_codes)").fetchall()}
            if "auto_created" not in ric_columns:
                conn.execute("ALTER TABLE ric_codes ADD COLUMN auto_created INTEGER NOT NULL DEFAULT 0")
            if "confidence" not in ric_columns:
                conn.execute("ALTER TABLE ric_codes ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")

            now = self._now()
            for item in DEFAULT_STATIONS:
                conn.execute(
                    """INSERT OR IGNORE INTO stations(
                           station_key, name, marker, active, auto_created, confidence, source, created_at, updated_at
                       ) VALUES (?, ?, ?, 1, 0, 1.0, 'seed', ?, ?)""",
                    (item["key"], item["name"], item["marker"], now, now),
                )

            # Existing admins retain the historic "see/receive everything" behaviour.
            admins = conn.execute("SELECT id FROM users WHERE role='admin' AND active=1").fetchall()
            conn.executemany(
                """INSERT OR IGNORE INTO user_routing_preferences(user_id, receive_all, updated_at)
                   VALUES (?, 1, ?)""",
                [(int(row["id"]), now) for row in admins],
            )
            conn.commit()

    @staticmethod
    def normalize_ric(value: Any) -> str:
        ric = str(value or "").strip()
        if not ric.isdigit() or not 4 <= len(ric) <= 10:
            raise ValueError("RIC/capcode skal være 4-10 cifre")
        return ric

    @staticmethod
    def normalize_station_name(value: Any) -> str:
        name = " ".join(str(value or "").strip().split())
        if not STATION_NAME_RE.fullmatch(name):
            raise ValueError("Ugyldigt stations-/områdenavn")
        return name

    @classmethod
    def automatic_key(cls, name: str) -> str:
        digest = hashlib.sha1(name.casefold().encode("utf-8")).hexdigest()[:10]
        return f"AUTO-{digest}"

    def list_stations(self, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            where = "WHERE s.active=1" if active_only else ""
            rows = conn.execute(
                f"""SELECT s.station_key AS key, s.name, s.marker, s.active,
                           s.auto_created, s.confidence, s.source, s.created_at, s.updated_at,
                           COUNT(DISTINCT r.id) AS ric_count,
                           COUNT(DISTINCT m.id) AS message_count
                    FROM stations s
                    LEFT JOIN ric_codes r ON r.station_key=s.station_key
                    LEFT JOIN messages m ON m.station=s.name
                    {where}
                    GROUP BY s.station_key
                    ORDER BY s.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def all_station_keys(self) -> list[str]:
        return [str(row["key"]) for row in self.list_stations(active_only=True)]

    def get_station(self, key: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT station_key AS key, * FROM stations WHERE station_key=?", (str(key),)
            ).fetchone()
        return dict(row) if row else None

    def station_name(self, key: str | None) -> str | None:
        if not key:
            return None
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT name FROM stations WHERE station_key=? AND active=1", (str(key),)
            ).fetchone()
        return str(row["name"]) if row else None

    def station_key(self, name: str | None) -> str | None:
        clean = " ".join(str(name or "").strip().split())
        if not clean:
            return None
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT station_key FROM stations WHERE name=? COLLATE NOCASE AND active=1", (clean,)
            ).fetchone()
        return str(row["station_key"]) if row else None

    def create_station(self, name: Any, *, marker: Any = None, auto_created: bool = False,
                       confidence: float = 1.0, source: str = "admin") -> dict[str, Any]:
        clean_name = self.normalize_station_name(name)
        marker_value = str(marker or "").strip().upper()[:12] or None
        key = self.automatic_key(clean_name)
        if marker_value and len(marker_value) <= 3 and marker_value.isalnum():
            # Keep seeded one-letter keys stable, while user-created stations remain hash-keyed.
            existing_marker_key = marker_value if self.get_station(marker_value) else None
            if existing_marker_key and self.station_name(existing_marker_key) == clean_name:
                key = existing_marker_key
        now = self._now()
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO stations(station_key, name, marker, active, auto_created,
                                          confidence, source, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       active=1,
                       confidence=MAX(stations.confidence, excluded.confidence),
                       updated_at=excluded.updated_at""",
                (key, clean_name, marker_value, 1 if auto_created else 0,
                 max(0.0, min(float(confidence), 1.0)), str(source)[:40], now, now),
            )
            row = conn.execute(
                "SELECT station_key AS key, * FROM stations WHERE name=? COLLATE NOCASE", (clean_name,)
            ).fetchone()
            conn.commit()
        return dict(row)

    def update_station(self, key: str, *, name: Any | None = None, active: bool | None = None) -> dict[str, Any] | None:
        current = self.get_station(key)
        if not current:
            return None
        clean_name = current["name"] if name is None else self.normalize_station_name(name)
        enabled = bool(current["active"]) if active is None else bool(active)
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE stations SET name=?, active=?, updated_at=? WHERE station_key=?",
                (clean_name, 1 if enabled else 0, self._now(), key),
            )
            if clean_name != current["name"]:
                conn.execute("UPDATE messages SET station=? WHERE station=?", (clean_name, current["name"]))
            conn.commit()
        return self.get_station(key)

    def normalize_station_keys(self, values: Iterable[Any]) -> list[str]:
        supplied: list[str] = []
        for raw in values:
            key = str(raw or "").strip()
            if key and key not in supplied:
                supplied.append(key)
        if not supplied:
            return []
        placeholders = ",".join("?" for _ in supplied)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT station_key FROM stations WHERE active=1 AND station_key IN ({placeholders})",
                supplied,
            ).fetchall()
        valid = {str(row["station_key"]) for row in rows}
        if any(key not in valid for key in supplied):
            raise ValueError("Ukendt station/område")
        return supplied

    def _extract_explicit_station_name(self, text: str) -> str | None:
        for pattern in EXPLICIT_STATION_PATTERNS:
            match = pattern.search(str(text or ""))
            if match:
                candidate = " ".join(match.group(1).strip().split())
                try:
                    return self.normalize_station_name(candidate)
                except ValueError:
                    continue
        return None

    def _observe_station_discovery(self, ric: str | None, message: str) -> tuple[str | None, str | None]:
        candidate = self._extract_explicit_station_name(message)
        if not candidate:
            return None, None
        clean_ric = str(ric or "").strip()
        now = self._now()
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO station_discovery_evidence(
                       candidate_name, ric, seen_count, first_seen_at, last_seen_at, sample_message
                   ) VALUES (?, ?, 1, ?, ?, ?)
                   ON CONFLICT(candidate_name, ric) DO UPDATE SET
                       seen_count=station_discovery_evidence.seen_count+1,
                       last_seen_at=excluded.last_seen_at,
                       sample_message=excluded.sample_message""",
                (candidate, clean_ric, now, now, str(message)[:500]),
            )
            total_row = conn.execute(
                "SELECT SUM(seen_count) AS c FROM station_discovery_evidence WHERE candidate_name=? COLLATE NOCASE",
                (candidate,),
            ).fetchone()
            ric_row = conn.execute(
                "SELECT seen_count AS c FROM station_discovery_evidence WHERE candidate_name=? COLLATE NOCASE AND ric=?",
                (candidate, clean_ric),
            ).fetchone()
            conn.commit()
        total = int(total_row["c"] or 0)
        ric_count = int(ric_row["c"] or 0) if ric_row else 0

        station = self.station_key(candidate)
        if not station and total >= 3:
            created = self.create_station(
                candidate, auto_created=True,
                confidence=min(0.99, 0.70 + min(total, 10) * 0.03),
                source="adaptive-explicit-text",
            )
            station = str(created["key"])

        # Only learn a RIC automatically after repeated explicit station evidence.
        if station and clean_ric and ric_count >= 5:
            with self._lock, self.connect() as conn:
                existing = conn.execute("SELECT id FROM ric_codes WHERE ric=?", (clean_ric,)).fetchone()
                if not existing:
                    now = self._now()
                    conn.execute(
                        """INSERT INTO ric_codes(
                               ric, station_key, label, active, created_at, updated_at,
                               created_by, auto_created, confidence
                           ) VALUES (?, ?, 'Automatisk lært', 1, ?, ?, NULL, 1, ?)""",
                        (clean_ric, station, now, now, min(0.99, 0.75 + ric_count * 0.03)),
                    )
                    conn.commit()
        return self.station_name(station) if station else None, candidate

    def classify(self, ric: str | None, fallback_station: str | None,
                 message: str = "") -> tuple[str | None, str]:
        clean_ric = str(ric or "").strip()
        if clean_ric:
            with self._lock, self.connect() as conn:
                row = conn.execute(
                    "SELECT station_key FROM ric_codes WHERE ric=? AND active=1", (clean_ric,)
                ).fetchone()
            if row:
                name = self.station_name(row["station_key"])
                if name:
                    return name, "ric"

        fallback_key = self.station_key(fallback_station)
        if fallback_key:
            return self.station_name(fallback_key), "marker"

        learned_station, _candidate = self._observe_station_discovery(clean_ric, message)
        if learned_station:
            return learned_station, "adaptive-explicit-text"
        return None, "unknown"

    def list_station_suggestions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT candidate_name, SUM(seen_count) AS seen_count,
                          MAX(last_seen_at) AS last_seen_at,
                          MAX(sample_message) AS sample_message,
                          COUNT(DISTINCT CASE WHEN ric!='' THEN ric END) AS ric_count
                   FROM station_discovery_evidence e
                   WHERE NOT EXISTS (
                       SELECT 1 FROM stations s WHERE s.name=e.candidate_name COLLATE NOCASE
                   )
                   GROUP BY candidate_name
                   ORDER BY SUM(seen_count) DESC, MAX(last_seen_at) DESC LIMIT ?""",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_ric_codes(self) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT r.id, r.ric, r.station_key, r.label, r.active, r.auto_created,
                          r.confidence, r.created_at, r.updated_at, s.name AS station,
                          COUNT(m.id) AS message_count, MAX(m.received_at) AS last_seen
                   FROM ric_codes r JOIN stations s ON s.station_key=r.station_key
                   LEFT JOIN messages m ON m.ric=r.ric
                   GROUP BY r.id ORDER BY s.name COLLATE NOCASE, r.ric"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_ric_code(self, ric_id: int) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                """SELECT r.*, s.name AS station FROM ric_codes r
                   JOIN stations s ON s.station_key=r.station_key WHERE r.id=?""", (ric_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_ric_code(self, ric: Any, station_key: Any, label: Any,
                        active: bool, created_by: int | None) -> int:
        clean_ric = self.normalize_ric(ric)
        key = self.normalize_station_keys([station_key])[0]
        clean_label = str(label or "").strip()[:120]
        now = self._now()
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO ric_codes(
                       ric, station_key, label, active, created_at, updated_at, created_by, auto_created, confidence
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1.0)""",
                (clean_ric, key, clean_label, 1 if active else 0, now, now, created_by),
            )
            if active:
                conn.execute("UPDATE messages SET station=? WHERE ric=?", (self.station_name(key), clean_ric))
            conn.commit()
            return int(cur.lastrowid)

    def update_ric_code(self, ric_id: int, *, ric: Any | None = None,
                        station_key: Any | None = None, label: Any | None = None,
                        active: bool | None = None) -> dict[str, Any] | None:
        current = self.get_ric_code(ric_id)
        if not current:
            return None
        clean_ric = self.normalize_ric(current["ric"] if ric is None else ric)
        key = self.normalize_station_keys([current["station_key"] if station_key is None else station_key])[0]
        clean_label = str(current["label"] if label is None else label or "").strip()[:120]
        enabled = bool(current["active"]) if active is None else bool(active)
        with self._lock, self.connect() as conn:
            conn.execute(
                """UPDATE ric_codes SET ric=?, station_key=?, label=?, active=?,
                                          auto_created=0, confidence=1.0, updated_at=? WHERE id=?""",
                (clean_ric, key, clean_label, 1 if enabled else 0, self._now(), ric_id),
            )
            if enabled:
                conn.execute("UPDATE messages SET station=? WHERE ric=?", (self.station_name(key), clean_ric))
            conn.commit()
        return self.get_ric_code(ric_id)

    def delete_ric_code(self, ric_id: int) -> bool:
        with self._lock, self.connect() as conn:
            cur = conn.execute("DELETE FROM ric_codes WHERE id=?", (ric_id,))
            conn.commit()
            return cur.rowcount > 0

    def list_unknown_rics(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT m.ric, COUNT(*) AS message_count, MAX(m.received_at) AS last_seen,
                          (SELECT m2.message FROM messages m2 WHERE m2.ric=m.ric ORDER BY m2.id DESC LIMIT 1) AS sample_message
                   FROM messages m
                   WHERE m.ric IS NOT NULL AND TRIM(m.ric) != ''
                     AND NOT EXISTS (SELECT 1 FROM ric_codes r WHERE r.ric=m.ric)
                   GROUP BY m.ric ORDER BY MAX(m.id) DESC LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_user_stations(self, user_id: int, station_keys: Iterable[Any]) -> list[str]:
        keys = self.normalize_station_keys(station_keys)
        now = self._now()
        with self._lock, self.connect() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise ValueError("Brugeren findes ikke")
            conn.execute("DELETE FROM user_station_subscriptions WHERE user_id=?", (user_id,))
            conn.executemany(
                "INSERT INTO user_station_subscriptions(user_id, station_key, created_at) VALUES (?, ?, ?)",
                [(user_id, key, now) for key in keys],
            )
            conn.commit()
        return keys

    def set_user_receive_all(self, user_id: int, receive_all: bool) -> bool:
        with self._lock, self.connect() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise ValueError("Brugeren findes ikke")
            conn.execute(
                """INSERT INTO user_routing_preferences(user_id, receive_all, updated_at)
                   VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                       receive_all=excluded.receive_all, updated_at=excluded.updated_at""",
                (user_id, 1 if receive_all else 0, self._now()),
            )
            conn.commit()
        return bool(receive_all)

    def user_receive_all(self, user_id: int) -> bool:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT receive_all FROM user_routing_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
        return bool(row and row["receive_all"])

    def user_stations(self, user_id: int) -> list[str]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT station_key FROM user_station_subscriptions WHERE user_id=? ORDER BY station_key",
                (user_id,),
            ).fetchall()
        return [str(row["station_key"]) for row in rows]

    def attach_user_stations(self, users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        station_map = {row["key"]: row["name"] for row in self.list_stations()}
        for user in users:
            keys = self.user_stations(int(user["id"]))
            user["stations"] = keys
            user["station_names"] = [station_map[key] for key in keys if key in station_map]
            user["receive_all"] = self.user_receive_all(int(user["id"]))
        return users

    @staticmethod
    def _public_columns() -> str:
        # Deliberately excludes RIC, raw_line and decoder function. Those are admin-only data.
        return "id, received_at, protocol, baud, station, message, source, relevance_class, relevance_score"

    def list_messages_for_user(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self.connect() as conn:
            if self.user_receive_all(user_id):
                rows = conn.execute(
                    f"SELECT {self._public_columns()} FROM messages WHERE delivery_eligible=1 ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]

            keys = self.user_stations(user_id)
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"""SELECT {self._public_columns()} FROM messages
                    WHERE delivery_eligible=1 AND station IN (
                        SELECT name FROM stations WHERE station_key IN ({placeholders}) AND active=1
                    ) ORDER BY id DESC LIMIT ?""",
                (*keys, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_push_subscriptions_for_event(self, station: str | None,
                                          delivery_eligible: bool = True) -> list[dict[str, Any]]:
        if not delivery_eligible:
            return []
        key = self.station_key(station)
        with self._lock, self.connect() as conn:
            if key:
                rows = conn.execute(
                    """SELECT DISTINCT ps.* FROM push_subscriptions ps
                       JOIN users u ON u.id=ps.user_id
                       LEFT JOIN user_routing_preferences p ON p.user_id=u.id
                       LEFT JOIN user_station_subscriptions us ON us.user_id=u.id
                       WHERE u.active=1 AND (COALESCE(p.receive_all,0)=1 OR us.station_key=?)
                       ORDER BY ps.id""",
                    (key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT DISTINCT ps.* FROM push_subscriptions ps
                       JOIN users u ON u.id=ps.user_id
                       LEFT JOIN user_routing_preferences p ON p.user_id=u.id
                       WHERE u.active=1 AND (u.role='admin' OR COALESCE(p.receive_all,0)=1)
                       ORDER BY ps.id"""
                ).fetchall()
        return [dict(row) for row in rows]
