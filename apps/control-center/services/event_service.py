from datetime import datetime, timedelta, timezone


def append_event(
    event_key,
    severity,
    title,
    message,
    database_factory,
    recorded_at=None,
    cooldown=timedelta(hours=1),
    retention=timedelta(days=30),
):
    timestamp = recorded_at or datetime.now(timezone.utc)
    with database_factory() as connection:
        latest = connection.execute(
            "SELECT recorded_at FROM events WHERE event_key = ? ORDER BY id DESC LIMIT 1",
            (event_key,),
        ).fetchone()
        if latest and timestamp - datetime.fromisoformat(latest["recorded_at"]) < cooldown:
            return False

        connection.execute(
            "INSERT INTO events (recorded_at, event_key, severity, title, message) VALUES (?, ?, ?, ?, ?)",
            (
                timestamp.isoformat(),
                event_key,
                severity,
                title,
                message[:500],
            ),
        )
        connection.execute(
            "DELETE FROM events WHERE recorded_at < ?",
            ((timestamp - retention).isoformat(),),
        )
    return True


def list_events(limit, database_factory):
    with database_factory() as connection:
        rows = connection.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]
