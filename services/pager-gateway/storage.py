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
        filename = str(data.get("filename") or "").strip()
        if not BACKUP_NAME_RE.fullmatch(filename):
            raise ValueError("invalid backup filename")
        return {"filename": filename}
    return {}


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
            conn.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
                    notification_sent INTEGER NOT NULL DEFAULT 0,
                    message_fingerprint TEXT,
                    relevance_class TEXT NOT NULL DEFAULT 'unknown',
                    relevance_score REAL NOT NULL DEFAULT 0.75,
                    suppressed_reason TEXT,
                    duplicate_of INTEGER,
                    delivery_eligible INTEGER NOT NULL DEFAULT 1,
                    decision_reason TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(duplicate_of) REFERENCES messages(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_station ON messages(station);

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    created_by INTEGER,
                    last_login_at TEXT,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_push_user_id ON push_subscriptions(user_id);

                CREATE TABLE IF NOT EXISTS system_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    requested_by INTEGER NOT NULL,
                    requested_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    processed_at TEXT,
                    result TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(requested_by) REFERENCES users(id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_system_commands_status ON system_commands(status, id);

                CREATE TABLE IF NOT EXISTS runtime_status (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC);
                """
            )

            system_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(system_commands)").fetchall()}
            if "payload" not in system_columns:
                conn.execute("ALTER TABLE system_commands ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")

            message_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            migrations = {
                "message_fingerprint": "TEXT",
                "relevance_class": "TEXT NOT NULL DEFAULT 'unknown'",
                "relevance_score": "REAL NOT NULL DEFAULT 0.75",
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
                    source, notification_sent, message_fingerprint, relevance_class,
                    relevance_score, suppressed_reason, duplicate_of, delivery_eligible, decision_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    received_at, event.get("protocol", "POCSAG"), event.get("baud"),
                    event.get("ric"), event.get("function"), event.get("station"),
                    event.get("message", ""), event.get("raw_line", event.get("message", "")),
                    event.get("source", "unknown"), 1 if event.get("notification_sent") else 0,
                    event.get("message_fingerprint"), event.get("relevance_class", "unknown"),
                    float(event.get("relevance_score", 0.75)), event.get("suppressed_reason"),
                    event.get("duplicate_of"), 1 if event.get("delivery_eligible", True) else 0,
                    str(event.get("decision_reason") or "")[:500],
                ),
            )
            return int(cur.lastrowid)

    def mark_notification_sent(self, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE messages SET notification_sent=1 WHERE id=?", (message_id,))

    def list_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def latest_message(self) -> dict[str, Any] | None:
        rows = self.list_messages(limit=1)
        return rows[0] if rows else None

    def message_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
        return int(row["count"])

    def user_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def create_user(self, username: str, display_name: str, password_hash: str,
                    role: str, created_by: int | None = None) -> int:
        if role not in {"admin", "user"}:
            raise ValueError("invalid role")
        now = self._now()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO users(username, display_name, password_hash, role, active, created_at, created_by)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (username.strip(), display_name.strip(), password_hash, role, now, created_by),
            )
            user_id = int(cur.lastrowid)
            if created_by is not None:
                conn.execute(
                    "INSERT INTO audit_log(user_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                    (created_by, "user-create", f"user_id={user_id}; role={role}", now),
                )
            return user_id

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.role, u.active,
                          u.created_at, u.last_login_at, COUNT(ps.id) AS push_devices
                   FROM users u LEFT JOIN push_subscriptions ps ON ps.user_id=u.id
                   GROUP BY u.id ORDER BY u.role, u.display_name COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_login(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (self._now(), user_id))

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
            if not active:
                conn.execute("DELETE FROM push_subscriptions WHERE user_id=?", (user_id,))

    def set_user_password_hash(self, user_id: int, password_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))

    def upsert_push_subscription(self, user_id: int, endpoint: str, p256dh: str,
                                 auth: str, user_agent: str = "") -> None:
        now = self._now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO push_subscriptions(user_id, endpoint, p256dh, auth, user_agent, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh,
                       auth=excluded.auth, user_agent=excluded.user_agent, last_seen_at=excluded.last_seen_at""",
                (user_id, endpoint, p256dh, auth, user_agent, now, now),
            )

    def delete_push_subscription(self, endpoint: str, user_id: int | None = None) -> None:
        with self.connect() as conn:
            if user_id is None:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
            else:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?", (endpoint, user_id))

    def list_active_push_subscriptions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ps.* FROM push_subscriptions ps JOIN users u ON u.id=ps.user_id WHERE u.active=1 ORDER BY ps.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_user_push_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM push_subscriptions WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, user_id: int | None, action: str, detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(user_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (user_id, str(action)[:80], str(detail)[:1000], self._now()),
            )

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.id, a.user_id, a.action, a.detail, a.created_at, u.username AS username
                   FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def queue_system_command(self, action: str, requested_by: int,
                             payload: dict[str, Any] | None = None) -> int:
        normalized = validate_system_command(action, payload)
        now = self._now()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO system_commands(action, requested_by, requested_at, status, payload)
                   VALUES (?, ?, ?, 'queued', ?)""",
                (action, requested_by, now, json.dumps(normalized, ensure_ascii=False)),
            )
            command_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO audit_log(user_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (requested_by, "system-command", f"action={action}; command_id={command_id}", now),
            )
            return command_id

    def list_system_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT sc.id, sc.action, sc.requested_by, sc.requested_at, sc.status,
                          sc.processed_at, sc.result, u.username AS requested_by_username
                   FROM system_commands sc JOIN users u ON u.id=sc.requested_by
                   ORDER BY sc.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next_system_command(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM system_commands WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            updated = conn.execute(
                "UPDATE system_commands SET status='processing' WHERE id=? AND status='queued'", (row["id"],)
            )
            if updated.rowcount != 1:
                return None
            result = dict(row)
            try:
                result["payload"] = json.loads(str(result.get("payload") or "{}"))
            except json.JSONDecodeError:
                result["payload"] = {}
            return result

    def finish_system_command(self, command_id: int, success: bool, result: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE system_commands SET status=?, processed_at=?, result=?, payload='{}' WHERE id=?",
                ("done" if success else "failed", self._now(), result[:2000], command_id),
            )

    def update_runtime_status(self, values: dict[str, Any]) -> None:
        updated_at = self._now()
        with self.connect() as conn:
            for key, value in values.items():
                conn.execute(
                    """INSERT INTO runtime_status(key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    (str(key), str(value), updated_at),
                )

    def get_runtime_status(self) -> dict[str, dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM runtime_status ORDER BY key").fetchall()
        return {str(row["key"]): {"value": str(row["value"]), "updated_at": str(row["updated_at"])} for row in rows}
