from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

APP_DIR_NAME = "SBR Pager Gateway"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    root = Path(base) if base else Path.home() / ".sbr-pager-gateway"
    path = root / APP_DIR_NAME if base else root
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "sbr-pager-gateway.db"


@contextmanager
def connection():
    db = sqlite3.connect(database_path(), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_database() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS allowed_senders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL UNIQUE,
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at TEXT NOT NULL,
                modem_port TEXT,
                accepted INTEGER NOT NULL DEFAULT 0,
                processing_status TEXT NOT NULL DEFAULT 'received',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS whatsapp_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbound_id INTEGER,
                recipient_name TEXT NOT NULL,
                recipient_phone TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id TEXT,
                error TEXT,
                attempted_at TEXT NOT NULL,
                FOREIGN KEY(inbound_id) REFERENCES inbound_messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS gateway_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inbound_received_at
                ON inbound_messages(received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_delivery_inbound
                ON whatsapp_deliveries(inbound_id);
            CREATE INDEX IF NOT EXISTS idx_events_created_at
                ON gateway_events(created_at DESC);
            """
        )


def get_setting(key: str, default: str | None = None) -> str | None:
    with connection() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    with connection() as db:
        db.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, utcnow_iso()),
        )


def add_event(level: str, event_type: str, message: str) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO gateway_events(level, event_type, message, created_at) VALUES(?,?,?,?)",
            (level, event_type, message, utcnow_iso()),
        )


def store_inbound_message(
    *,
    source_id: str,
    sender: str,
    body: str,
    received_at: str,
    modem_port: str,
    processing_status: str = "received",
) -> tuple[int, bool]:
    with connection() as db:
        existing = db.execute(
            "SELECT id FROM inbound_messages WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if existing:
            return int(existing["id"]), False

        cursor = db.execute(
            """
            INSERT INTO inbound_messages(
                source_id, sender, body, received_at, modem_port,
                accepted, processing_status, created_at
            ) VALUES(?,?,?,?,?,0,?,?)
            """,
            (
                source_id,
                sender,
                body,
                received_at,
                modem_port,
                processing_status,
                utcnow_iso(),
            ),
        )
        return int(cursor.lastrowid), True


def set_message_status(message_id: int, status: str, accepted: bool | None = None) -> None:
    with connection() as db:
        if accepted is None:
            db.execute(
                "UPDATE inbound_messages SET processing_status=? WHERE id=?",
                (status, message_id),
            )
        else:
            db.execute(
                "UPDATE inbound_messages SET processing_status=?, accepted=? WHERE id=?",
                (status, 1 if accepted else 0, message_id),
            )


def recent_messages(limit: int = 100) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT
                m.id, m.sender, m.body, m.received_at, m.accepted,
                m.processing_status, m.modem_port,
                COALESCE(SUM(CASE WHEN d.status='sent' THEN 1 ELSE 0 END), 0) AS sent_count,
                COALESCE(SUM(CASE WHEN d.status='failed' THEN 1 ELSE 0 END), 0) AS failed_count
            FROM inbound_messages m
            LEFT JOIN whatsapp_deliveries d ON d.inbound_id=m.id
            GROUP BY m.id
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]


def message_count() -> int:
    with connection() as db:
        row = db.execute("SELECT COUNT(*) AS count FROM inbound_messages").fetchone()
        return int(row["count"])


def list_allowed_senders() -> list[dict]:
    with connection() as db:
        rows = db.execute(
            "SELECT id, name, phone, active, created_at FROM allowed_senders ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(row) for row in rows]


def save_allowed_sender(name: str, phone: str) -> int:
    with connection() as db:
        cursor = db.execute(
            "INSERT INTO allowed_senders(name, phone, active, created_at) VALUES(?,?,1,?)",
            (name.strip(), phone, utcnow_iso()),
        )
        return int(cursor.lastrowid)


def set_allowed_sender_active(sender_id: int, active: bool) -> None:
    with connection() as db:
        db.execute(
            "UPDATE allowed_senders SET active=? WHERE id=?",
            (1 if active else 0, sender_id),
        )


def delete_allowed_sender(sender_id: int) -> None:
    with connection() as db:
        db.execute("DELETE FROM allowed_senders WHERE id=?", (sender_id,))


def is_sender_allowed(phone: str) -> bool:
    with connection() as db:
        row = db.execute(
            "SELECT 1 FROM allowed_senders WHERE phone=? AND active=1 LIMIT 1",
            (phone,),
        ).fetchone()
        return row is not None


def list_recipients(active_only: bool = False) -> list[dict]:
    with connection() as db:
        sql = "SELECT id, name, phone, active, created_at FROM recipients"
        params: tuple = ()
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY name COLLATE NOCASE"
        rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def save_recipient(name: str, phone: str) -> int:
    with connection() as db:
        cursor = db.execute(
            "INSERT INTO recipients(name, phone, active, created_at) VALUES(?,?,1,?)",
            (name.strip(), phone, utcnow_iso()),
        )
        return int(cursor.lastrowid)


def set_recipient_active(recipient_id: int, active: bool) -> None:
    with connection() as db:
        db.execute(
            "UPDATE recipients SET active=? WHERE id=?",
            (1 if active else 0, recipient_id),
        )


def delete_recipient(recipient_id: int) -> None:
    with connection() as db:
        db.execute("DELETE FROM recipients WHERE id=?", (recipient_id,))
