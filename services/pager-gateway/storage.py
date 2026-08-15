from __future__ import annotations

import json
import re
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
    "vapid_subject": "mailto:admin@racher.local",
    "duplicate_window_seconds": "30",
    "adaptive_filter_enabled": "1",
    "external_monitor_enabled": "0",
    "external_monitor_sms_to": "",
    "external_monitor_failure_threshold": "3",
    "external_monitor_access_key": "",
}

SYSTEM_ACTIONS = {
    "restart-pdl", "restart-gateway", "reboot", "backup-now", "restore-backup",
    "update-gateway", "rollback-gateway", "wifi-add", "wifi-remove",
    "hotspot-start", "hotspot-stop", "restart-tunnel",
}
BACKUP_NAME_RE = re.compile(r"^racher-pager-\d{8}T\d{6}Z\.tar\.gz$")
WIFI_PROFILE_RE = re.compile(r"^racher-wifi-[0-9a-f]{10}$")


def validate_system_command(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    action = str(action or "").strip()
    if action not in SYSTEM_ACTIONS:
        raise ValueError("invalid system action")
    data = payload if isinstance(payload, dict) else {}
    if action == "wifi-add":
        ssid = str(data.get("ssid") or "").strip()
        password = str(data.get("password") or "")
        if not 1 <= len(ssid) <= 32:
            raise ValueError("invalid Wi-Fi SSID")
        if not 8 <= len(password) <= 63:
            raise ValueError("invalid Wi-Fi password")
        return {"ssid": ssid, "password": password}
    if action == "wifi-remove":
        profile = str(data.get("profile") or "").strip()
        if not WIFI_PROFILE_RE.fullmatch(profile):
            raise ValueError("invalid Wi-Fi profile")
        return {"profile": profile}
    if action == "restore-backup":
        backup = str(data.get("backup") or "").strip()
        if not BACKUP_NAME_RE.fullmatch(backup):
            raise ValueError("invalid backup name")
        return {"backup": backup}
    if action not in {"restart-pdl", "restart-gateway", "reboot", "backup-now", "update-gateway", "rollback-gateway", "hotspot-start", "hotspot-stop", "restart-tunnel"}:
        raise ValueError("unexpected payload")
    return {}


class Storage:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    protocol TEXT NOT NULL DEFAULT '',
                    baud TEXT NOT NULL DEFAULT '',
                    ric TEXT NOT NULL DEFAULT '',
                    function TEXT NOT NULL DEFAULT '',
                    station TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    raw_line TEXT NOT NULL,
                    is_noise INTEGER NOT NULL DEFAULT 0,
                    message_fingerprint TEXT NOT NULL DEFAULT '',
                    relevance_class TEXT NOT NULL DEFAULT 'unknown',
                    relevance_score REAL NOT NULL DEFAULT 0,
                    suppressed_reason TEXT,
                    duplicate_of INTEGER,
                    delivery_eligible INTEGER NOT NULL DEFAULT 1,
                    decision_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    active INTEGER NOT NULL DEFAULT 1,
                    receive_all INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stations (
                    key TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    normalized_name TEXT NOT NULL UNIQUE,
                    auto_created INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_stations (
                    user_id INTEGER NOT NULL,
                    station_key TEXT NOT NULL,
                    PRIMARY KEY(user_id, station_key),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(station_key) REFERENCES stations(key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS ric_routes (
                    ric TEXT PRIMARY KEY,
                    station_key TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(station_key) REFERENCES stations(key)
                );
                CREATE TABLE IF NOT EXISTS unknown_rics (
                    ric TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    last_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS station_evidence (
                    candidate_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    ric TEXT NOT NULL DEFAULT '',
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    sample_message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(normalized_name, ric)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_id INTEGER,
                    username TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS runtime_status (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    requested_by INTEGER,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT NOT NULL DEFAULT '',
                    completed_at TEXT
                );
                """
            )

            message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            migrations = {
                "is_noise": "INTEGER NOT NULL DEFAULT 0",
                "message_fingerprint": "TEXT NOT NULL DEFAULT ''",
                "relevance_class": "TEXT NOT NULL DEFAULT 'unknown'",
                "relevance_score": "REAL NOT NULL DEFAULT 0",
                "suppressed_reason": "TEXT",
                "duplicate_of": "INTEGER",
                "delivery_eligible": "INTEGER NOT NULL DEFAULT 1",
                "decision_reason": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in message_columns:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

            # Indexes referencing migrated columns must be created only after the
            # ALTER TABLE statements above. Otherwise an older pager.db fails to
            # open before it gets a chance to migrate.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_fingerprint ON messages(message_fingerprint, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_delivery ON messages(delivery_eligible, id DESC)"
            )

            for key, value in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
            conn.execute(
                "DELETE FROM settings WHERE key IN (?, ?, ?, ?, ?)",
                ("station_a_enabled", "station_s_enabled", "station_k_enabled", "station_l_enabled", "station_r_enabled"),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def update_settings(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    def add_message(self, event: dict[str, Any]) -> int:
        received_at = event.get("received_at") or self._now()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO messages(
                    received_at, protocol, baud, ric, function, station, message, raw_line,
                    is_noise, message_fingerprint, relevance_class, relevance_score,
                    suppressed_reason, duplicate_of, delivery_eligible, decision_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    received_at,
                    event.get("protocol", ""),
                    event.get("baud", ""),
                    event.get("ric", ""),
                    event.get("function", ""),
                    event.get("station", ""),
                    event.get("message", ""),
                    event.get("raw_line", ""),
                    1 if event.get("is_noise") else 0,
                    event.get("message_fingerprint", ""),
                    event.get("relevance_class", "unknown"),
                    float(event.get("relevance_score", 0) or 0),
                    event.get("suppressed_reason"),
                    event.get("duplicate_of"),
                    1 if event.get("delivery_eligible", True) else 0,
                    event.get("decision_reason", ""),
                ),
            )
            return int(cur.lastrowid)

    def message_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def list_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages_for_user(self, user_id: int, *, is_admin: bool, limit: int = 100) -> list[dict[str, Any]]:
        if is_admin:
            return self.list_messages(limit)
        with self.connect() as conn:
            user = conn.execute("SELECT receive_all FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
            if not user:
                return []
            if int(user["receive_all"] or 0):
                rows = conn.execute(
                    """SELECT m.* FROM messages m
                       WHERE m.delivery_eligible=1 AND m.is_noise=0 AND m.suppressed_reason IS NULL
                       ORDER BY m.id DESC LIMIT ?""",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT m.* FROM messages m
                       JOIN user_stations us ON us.station_key=m.station
                       WHERE us.user_id=? AND m.delivery_eligible=1 AND m.is_noise=0 AND m.suppressed_reason IS NULL
                       ORDER BY m.id DESC LIMIT ?""",
                    (user_id, max(1, min(limit, 1000))),
                ).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, *, user_id: int | None, username: str, action: str, target: str = "", detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(created_at, user_id, username, action, target, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (self._now(), user_id, username, action, target, detail),
            )

    def runtime_set(self, key: str, value: Any) -> None:
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runtime_status(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, payload, self._now()),
            )

    def runtime_get(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM runtime_status WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def runtime_all(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM runtime_status").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                value = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                value = row["value"]
            result[row["key"]] = {"value": value, "updated_at": row["updated_at"]}
        return result

    def add_user(self, username: str, display_name: str, password_hash: str, role: str = "user") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, display_name, password_hash, role, self._now()),
            )
            return int(cur.lastrowid)

    def user_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, username, display_name, role, active, receive_all, created_at FROM users ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_user_routing(self, user_id: int, stations: list[str], receive_all: bool) -> None:
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
            if not exists:
                raise ValueError("user not found")
            conn.execute("UPDATE users SET receive_all=? WHERE id=?", (1 if receive_all else 0, user_id))
            conn.execute("DELETE FROM user_stations WHERE user_id=?", (user_id,))
            if not receive_all:
                for station_key in stations:
                    station = conn.execute("SELECT key FROM stations WHERE key=?", (station_key,)).fetchone()
                    if not station:
                        raise ValueError("unknown station")
                    conn.execute(
                        "INSERT OR IGNORE INTO user_stations(user_id, station_key) VALUES (?, ?)",
                        (user_id, station_key),
                    )

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))

    def list_user_station_keys(self, user_id: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT station_key FROM user_stations WHERE user_id=? ORDER BY station_key",
                (user_id,),
            ).fetchall()
        return [str(row["station_key"]) for row in rows]
