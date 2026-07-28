import re

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "email",
    "actor",
)


class DatabaseObjectNotFoundError(Exception):
    """Raised when a requested SQLite table is not available."""


def _quote_identifier(value):
    if not _IDENTIFIER.fullmatch(str(value or "")):
        raise DatabaseObjectNotFoundError(str(value or ""))
    return f'"{value}"'


def _is_sensitive(column_name):
    normalized = str(column_name or "").lower()
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def _safe_value(column_name, value):
    if value is None:
        return None
    if _is_sensitive(column_name):
        return "[REDACTED]"
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    text = str(value)
    return text if len(text) <= 2_000 else text[:2_000] + "…"


def list_tables(database_factory):
    with database_factory() as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        result = []
        for row in rows:
            name = row["name"]
            quoted = _quote_identifier(name)
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            result.append({"name": name, "rows": int(count)})
    return result


def table_details(table_name, database_factory):
    quoted = _quote_identifier(table_name)
    with database_factory() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if not exists:
            raise DatabaseObjectNotFoundError(table_name)

        columns = []
        for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall():
            columns.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "nullable": not bool(row["notnull"]),
                    "primary_key": bool(row["pk"]),
                    "default": row["dflt_value"],
                    "redacted": _is_sensitive(row["name"]),
                }
            )

        indexes = []
        for row in connection.execute(f"PRAGMA index_list({quoted})").fetchall():
            indexes.append(
                {
                    "name": row["name"],
                    "unique": bool(row["unique"]),
                    "origin": row["origin"],
                }
            )
        row_count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]

    return {
        "name": table_name,
        "rows": int(row_count),
        "columns": columns,
        "indexes": indexes,
    }


def table_rows(table_name, database_factory, *, page=1, page_size=50):
    quoted = _quote_identifier(table_name)
    bounded_page = max(1, int(page))
    bounded_size = max(1, min(int(page_size), 100))
    offset = (bounded_page - 1) * bounded_size

    with database_factory() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if not exists:
            raise DatabaseObjectNotFoundError(table_name)

        total = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        rows = connection.execute(
            f"SELECT * FROM {quoted} LIMIT ? OFFSET ?",
            (bounded_size, offset),
        ).fetchall()
        entries = [
            {key: _safe_value(key, row[key]) for key in row.keys()}
            for row in rows
        ]

    return {
        "table": table_name,
        "page": bounded_page,
        "page_size": bounded_size,
        "total": total,
        "rows": entries,
    }
