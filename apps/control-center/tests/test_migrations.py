import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.migration_service import applied_versions, run_migrations


def test_migrations_run_once_and_record_version():
    connection = sqlite3.connect(":memory:")
    migrations = (
        (
            1,
            "create example table",
            ("CREATE TABLE example (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",),
        ),
    )

    assert run_migrations(connection, migrations) == [1]
    assert run_migrations(connection, migrations) == []
    assert applied_versions(connection) == {1}
    assert connection.execute(
        "SELECT name FROM schema_migrations WHERE version = 1"
    ).fetchone()[0] == "create example table"


def test_failed_migration_is_not_recorded():
    connection = sqlite3.connect(":memory:")
    migrations = ((1, "broken", ("CREATE TABLE ok (id INTEGER)", "INVALID SQL")),)

    try:
        run_migrations(connection, migrations)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("Expected migration failure")

    assert applied_versions(connection) == set()
