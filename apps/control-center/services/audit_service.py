from datetime import datetime, timezone


def append_audit_entry(
    action,
    target,
    success,
    message,
    actor,
    database_factory,
    recorded_at=None,
):
    timestamp = recorded_at or datetime.now(timezone.utc)
    with database_factory() as connection:
        connection.execute(
            "INSERT INTO audit_log (recorded_at, actor, action, target, success, message) VALUES (?, ?, ?, ?, ?, ?)",
            (
                timestamp.isoformat(),
                actor,
                action,
                target,
                int(success),
                message[:500],
            ),
        )


def list_audit_entries(limit, database_factory):
    with database_factory() as connection:
        rows = connection.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]
