from __future__ import annotations

from station_events import ensure_station_event_schema
from storage import connection, utcnow_iso

DEFAULT_ALARM_STATIONS = ("A", "S", "L", "K", "R", "B")


def ensure_default_station_rules() -> None:
    """Create the six station codes used by the current SBR Pager web system."""
    ensure_station_event_schema()
    with connection() as db:
        for code in DEFAULT_ALARM_STATIONS:
            db.execute(
                """
                INSERT OR IGNORE INTO station_rules(name, aliases, active, created_at)
                VALUES(?, '', 1, ?)
                """,
                (code, utcnow_iso()),
            )
