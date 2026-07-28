from datetime import datetime, timezone

WORKER_NAME = "monitoring"


def utc_now():
    return datetime.now(timezone.utc)


def register_worker_start(database_factory, *, worker_name=WORKER_NAME, recorded_at=None):
    recorded_at = recorded_at or utc_now()
    timestamp = recorded_at.isoformat()
    with database_factory() as connection:
        connection.execute(
            """
            INSERT INTO worker_status (
                worker_name,
                started_at,
                last_heartbeat_at,
                last_success_at,
                last_error,
                consecutive_failures
            ) VALUES (?, ?, ?, NULL, NULL, 0)
            ON CONFLICT(worker_name) DO UPDATE SET
                started_at = excluded.started_at,
                last_heartbeat_at = excluded.last_heartbeat_at,
                last_error = NULL,
                consecutive_failures = 0
            """,
            (worker_name, timestamp, timestamp),
        )


def record_worker_heartbeat(
    database_factory,
    *,
    success,
    error="",
    worker_name=WORKER_NAME,
    recorded_at=None,
):
    recorded_at = recorded_at or utc_now()
    timestamp = recorded_at.isoformat()
    error = str(error)[:500]

    with database_factory() as connection:
        if success:
            connection.execute(
                """
                INSERT INTO worker_status (
                    worker_name,
                    started_at,
                    last_heartbeat_at,
                    last_success_at,
                    last_error,
                    consecutive_failures
                ) VALUES (?, ?, ?, ?, NULL, 0)
                ON CONFLICT(worker_name) DO UPDATE SET
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL,
                    consecutive_failures = 0
                """,
                (worker_name, timestamp, timestamp, timestamp),
            )
        else:
            connection.execute(
                """
                INSERT INTO worker_status (
                    worker_name,
                    started_at,
                    last_heartbeat_at,
                    last_success_at,
                    last_error,
                    consecutive_failures
                ) VALUES (?, ?, ?, NULL, ?, 1)
                ON CONFLICT(worker_name) DO UPDATE SET
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_error = excluded.last_error,
                    consecutive_failures = worker_status.consecutive_failures + 1
                """,
                (worker_name, timestamp, timestamp, error),
            )


def get_worker_status(
    database_factory,
    *,
    max_age_seconds,
    worker_name=WORKER_NAME,
    checked_at=None,
):
    checked_at = checked_at or utc_now()
    with database_factory() as connection:
        row = connection.execute(
            "SELECT * FROM worker_status WHERE worker_name = ?",
            (worker_name,),
        ).fetchone()

    if row is None:
        return {
            "worker_name": worker_name,
            "healthy": False,
            "status": "missing",
            "age_seconds": None,
            "started_at": None,
            "last_heartbeat_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
        }

    result = dict(row)
    heartbeat_at = datetime.fromisoformat(result["last_heartbeat_at"])
    age_seconds = max(0, round((checked_at - heartbeat_at).total_seconds()))
    healthy = age_seconds <= max_age_seconds
    result.update(
        {
            "healthy": healthy,
            "status": "healthy" if healthy else "stale",
            "age_seconds": age_seconds,
        }
    )
    return result
