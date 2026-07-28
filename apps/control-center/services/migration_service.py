from datetime import datetime, timezone

MIGRATIONS = ()


def _ensure_migration_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )"""
    )


def applied_versions(connection):
    _ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]) for row in rows}


def run_migrations(connection, migrations=MIGRATIONS):
    _ensure_migration_table(connection)
    applied = applied_versions(connection)
    completed = []

    for version, name, statements in sorted(migrations, key=lambda item: item[0]):
        if version in applied:
            continue
        if version <= 0:
            raise ValueError("Migration versions must be positive integers")
        with connection:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).isoformat()),
            )
        completed.append(version)
    return completed
