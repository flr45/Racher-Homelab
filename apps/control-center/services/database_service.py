import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5_000

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS metrics (
        recorded_at TEXT PRIMARY KEY,
        cpu REAL NOT NULL,
        ram REAL NOT NULL,
        disk REAL NOT NULL,
        temperature REAL,
        network_sent_mb REAL NOT NULL,
        network_recv_mb REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        success INTEGER NOT NULL,
        message TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        event_key TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL
    )""",
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_audit_recorded_at ON audit_log(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_key_id ON events(event_key, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_recorded_at ON events(recorded_at)",
)


def open_database(data_root, database_path):
    data_root = Path(data_root)
    database_path = Path(database_path)
    data_root.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        database_path,
        timeout=BUSY_TIMEOUT_MS / 1_000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        connection.close()
        raise RuntimeError("SQLite WAL mode could not be enabled")
    connection.execute("PRAGMA synchronous = NORMAL")

    for statement in (*SCHEMA_STATEMENTS, *INDEX_STATEMENTS):
        connection.execute(statement)
    connection.commit()
    return connection
