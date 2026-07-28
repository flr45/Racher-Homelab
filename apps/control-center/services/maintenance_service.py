from datetime import datetime, timezone, timedelta


DEFAULT_MESSAGE = "Racher OS er midlertidigt i vedligeholdelsestilstand."
MAX_DURATION_MINUTES = 24 * 60


def utc_now():
    return datetime.now(timezone.utc)


def _normalize_message(message):
    return (str(message or DEFAULT_MESSAGE).strip() or DEFAULT_MESSAGE)[:500]


def _row_to_status(row, now):
    if row is None:
        return {
            "enabled": False,
            "message": None,
            "enabled_at": None,
            "expires_at": None,
            "enabled_by": None,
            "remaining_seconds": 0,
        }

    expires_at = datetime.fromisoformat(row["expires_at"])
    enabled = bool(row["enabled"]) and expires_at > now
    remaining = max(0, int((expires_at - now).total_seconds())) if enabled else 0
    return {
        "enabled": enabled,
        "message": row["message"] if enabled else None,
        "enabled_at": row["enabled_at"] if enabled else None,
        "expires_at": row["expires_at"] if enabled else None,
        "enabled_by": row["enabled_by"] if enabled else None,
        "remaining_seconds": remaining,
    }


def maintenance_status(database_factory, *, checked_at=None):
    now = checked_at or utc_now()
    with database_factory() as connection:
        row = connection.execute(
            "SELECT * FROM maintenance_mode WHERE id = 1"
        ).fetchone()
        status = _row_to_status(row, now)
        if row is not None and row["enabled"] and not status["enabled"]:
            connection.execute(
                "UPDATE maintenance_mode SET enabled = 0, disabled_at = ?, disabled_by = ? WHERE id = 1",
                (now.isoformat(), "automatic-expiry"),
            )
    return status


def enable_maintenance(message, duration_minutes, actor, database_factory, *, enabled_at=None):
    duration_minutes = int(duration_minutes)
    if duration_minutes < 1 or duration_minutes > MAX_DURATION_MINUTES:
        raise ValueError(f"Varighed skal være mellem 1 og {MAX_DURATION_MINUTES} minutter.")

    now = enabled_at or utc_now()
    expires_at = now + timedelta(minutes=duration_minutes)
    normalized_message = _normalize_message(message)
    normalized_actor = (str(actor or "unknown").strip() or "unknown")[:255]

    with database_factory() as connection:
        connection.execute(
            """INSERT INTO maintenance_mode (
                id, enabled, message, enabled_at, expires_at, enabled_by, disabled_at, disabled_by
            ) VALUES (1, 1, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(id) DO UPDATE SET
                enabled = 1,
                message = excluded.message,
                enabled_at = excluded.enabled_at,
                expires_at = excluded.expires_at,
                enabled_by = excluded.enabled_by,
                disabled_at = NULL,
                disabled_by = NULL""",
            (normalized_message, now.isoformat(), expires_at.isoformat(), normalized_actor),
        )

    return maintenance_status(database_factory, checked_at=now)


def disable_maintenance(actor, database_factory, *, disabled_at=None):
    now = disabled_at or utc_now()
    normalized_actor = (str(actor or "unknown").strip() or "unknown")[:255]
    with database_factory() as connection:
        connection.execute(
            """INSERT INTO maintenance_mode (
                id, enabled, message, enabled_at, expires_at, enabled_by, disabled_at, disabled_by
            ) VALUES (1, 0, NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled = 0,
                disabled_at = excluded.disabled_at,
                disabled_by = excluded.disabled_by""",
            (now.isoformat(), normalized_actor),
        )
    return maintenance_status(database_factory, checked_at=now)
