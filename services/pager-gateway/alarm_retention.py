from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


PUBLIC_COLUMNS = (
    "id, received_at, protocol, baud, station, message, source, "
    "relevance_class, relevance_score"
)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def retention_days() -> int:
    return _bounded_int("PAGER_ALARM_FEED_DAYS", 7, 1, 31)


def max_feed_rows() -> int:
    return _bounded_int("PAGER_ALARM_FEED_MAX_ROWS", 2000, 100, 5000)


def local_timezone() -> ZoneInfo:
    name = os.getenv("PAGER_LOCAL_TIMEZONE", "Europe/Copenhagen").strip() or "Europe/Copenhagen"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - invalid deployment timezone must degrade safely
        return ZoneInfo("Europe/Copenhagen")


def _parse_received_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        # PDW writes Danish local wall-clock timestamps without timezone metadata.
        moment = moment.replace(tzinfo=local_timezone())
    return moment.astimezone(timezone.utc)


def _within_window(value: Any, cutoff_utc: datetime) -> bool:
    moment = _parse_received_at(value)
    return moment is not None and moment >= cutoff_utc


def _sql_prefilter_cutoff(days: int) -> str:
    # Fetch one extra local day before the exact UTC filter. This keeps the query
    # tolerant of a mix of timezone-aware app timestamps and timezone-naive PDW
    # wall-clock timestamps, including DST transitions around the seven-day edge.
    local_cutoff = datetime.now(local_timezone()) - timedelta(days=days + 1)
    return local_cutoff.replace(tzinfo=None).isoformat(timespec="seconds")


def _recent_rows(core: Any, *, user_id: int | None = None) -> list[dict[str, Any]]:
    days = retention_days()
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=days)
    prefilter = _sql_prefilter_cutoff(days)

    parameters: list[Any] = [prefilter]
    if user_id is None:
        columns = "*"
        routing_clause = ""
    else:
        columns = PUBLIC_COLUMNS
        if core.routing.user_receive_all(user_id):
            routing_clause = ""
        else:
            station_keys = core.routing.user_stations(user_id)
            if not station_keys:
                return []
            placeholders = ",".join("?" for _ in station_keys)
            routing_clause = (
                " AND station IN ("
                "SELECT name FROM stations "
                f"WHERE station_key IN ({placeholders}) AND active=1"
                ")"
            )
            parameters.extend(station_keys)

    query = (
        f"SELECT {columns} FROM messages "
        "WHERE delivery_eligible=1 "
        "AND datetime(received_at) >= datetime(?)"
        f"{routing_clause} ORDER BY id DESC"
    )
    with core.storage.connect() as conn:
        rows = conn.execute(query, tuple(parameters)).fetchall()

    recent = [dict(row) for row in rows if _within_window(row["received_at"], cutoff_utc)]
    return recent[: max_feed_rows()]


def install_alarm_retention(core: Any):
    """Make the normal Alarmfeed a rolling seven-day archive.

    The underlying messages table is deliberately not pruned. Admin history,
    diagnostics and adaptive-learning evidence can therefore remain longer than
    the user-facing alarm window. Only delivery-eligible alarms appear here.
    """

    original_messages = core.app.view_functions["api_messages"]

    def recent_alarm_messages(*args: Any, **kwargs: Any):
        scope = str(core.request.args.get("scope") or "feed").strip().lower()
        if scope != "feed":
            return original_messages(*args, **kwargs)

        if not core.g.user:
            return core.jsonify({"ok": False, "error": "login required"}), 401

        if core.g.user["role"] == "admin":
            rows = _recent_rows(core)
        else:
            rows = _recent_rows(core, user_id=int(core.g.user["id"]))
        return core.jsonify(rows)

    core.app.view_functions["api_messages"] = recent_alarm_messages
    return recent_alarm_messages
