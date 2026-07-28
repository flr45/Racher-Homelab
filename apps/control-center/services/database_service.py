import sqlite3
from pathlib import Path

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


def open_database(data_root, database_path):
    data_root = Path(data_root)
    database_path = Path(database_path)
    data_root.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    return connection
