import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from database_browser_extension import init_database_browser
from services.database_browser_service import (
    DatabaseObjectNotFoundError,
    list_tables,
    table_details,
    table_rows,
)


def database_factory(path):
    def open_connection():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    return open_connection


def seed_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, token TEXT, name TEXT)"
        )
        connection.execute(
            "INSERT INTO users (email, token, name) VALUES (?, ?, ?)",
            ("user@example.test", "super-secret-token", "Frederik"),
        )
        connection.execute("CREATE INDEX idx_users_name ON users(name)")


def test_metadata_and_rows_are_read_only_and_redacted(tmp_path):
    path = tmp_path / "browser.db"
    seed_database(path)
    factory = database_factory(path)

    assert list_tables(factory) == [{"name": "users", "rows": 1}]
    details = table_details("users", factory)
    assert details["rows"] == 1
    assert any(column["name"] == "email" and column["redacted"] for column in details["columns"])
    assert any(index["name"] == "idx_users_name" for index in details["indexes"])

    snapshot = table_rows("users", factory, page=1, page_size=500)
    assert snapshot["page_size"] == 100
    assert snapshot["rows"] == [
        {
            "id": "1",
            "email": "[REDACTED]",
            "token": "[REDACTED]",
            "name": "Frederik",
        }
    ]
    assert "super-secret-token" not in repr(snapshot)
    assert "user@example.test" not in repr(snapshot)


def test_invalid_or_missing_table_is_rejected(tmp_path):
    path = tmp_path / "browser.db"
    seed_database(path)
    factory = database_factory(path)

    for name in ("../users", "users; DROP TABLE users", "missing"):
        try:
            table_rows(name, factory)
        except DatabaseObjectNotFoundError:
            pass
        else:
            raise AssertionError(f"Expected rejected table: {name}")


def test_api_dashboard_and_status(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "control-center.db",
        }
    )
    init_database_browser(app)
    client = app.test_client()

    tables = client.get("/api/database")
    assert tables.status_code == 200
    assert tables.get_json()["read_only"] is True

    rows = client.get("/api/database/audit_log/rows?page=1&page_size=25")
    assert rows.status_code == 200
    assert rows.get_json()["page_size"] == 25

    missing = client.get("/api/database/not_a_table")
    assert missing.status_code == 404

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["database_browser"]["read_only"] is True

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Database Browser" in dashboard.get_data(as_text=True)
