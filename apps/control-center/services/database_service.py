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
    """CREATE TABLE IF NOT EXISTS worker_status (
        worker_name TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        last_heartbeat_at TEXT NOT NULL,
        last_success_at TEXT,
        last_error TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        event_key TEXT NOT NULL,
        channel TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        sent_at TEXT,
        last_error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS deployments (
        container_name TEXT PRIMARY KEY,
        compose_project TEXT,
        compose_service TEXT,
        image_reference TEXT,
        image_id TEXT,
        image_digest TEXT,
        container_id TEXT,
        container_created_at TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        last_change_at TEXT NOT NULL,
        status TEXT,
        health TEXT,
        present INTEGER NOT NULL DEFAULT 1,
        missing_since TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS deployment_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        container_name TEXT NOT NULL,
        compose_project TEXT,
        compose_service TEXT,
        change_type TEXT NOT NULL,
        previous_image_id TEXT,
        image_id TEXT,
        previous_container_id TEXT,
        container_id TEXT,
        image_reference TEXT,
        status TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rollout_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        container_name TEXT NOT NULL,
        previous_image TEXT NOT NULL,
        target_image TEXT NOT NULL,
        status TEXT NOT NULL,
        phase TEXT NOT NULL,
        automatic_rollback INTEGER NOT NULL DEFAULT 1,
        message TEXT
    )""",
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_audit_recorded_at ON audit_log(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_key_id ON events(event_key, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_recorded_at ON events(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_dispatch ON notifications(status, next_attempt_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_event_channel ON notifications(event_key, channel, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deployments_present_project ON deployments(present, compose_project, compose_service)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_history_recorded_at ON deployment_history(recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_history_container ON deployment_history(container_name, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rollout_jobs_container_status ON rollout_jobs(container_name, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rollout_jobs_created_at ON rollout_jobs(created_at DESC)",
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
