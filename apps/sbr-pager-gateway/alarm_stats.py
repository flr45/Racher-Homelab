from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from storage import connection

ALLOWED_PERIODS = {30, 90, 365}
WEEKDAYS_DA = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(sum(values) / len(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }


def _pct(part: int, total: int) -> float:
    return round(part / total * 100, 1) if total else 0.0


def statistics_snapshot(days: int = 30) -> dict:
    days = days if days in ALLOWED_PERIODS else 30
    now = datetime.now(timezone.utc)
    local_now = now.astimezone()
    start = now - timedelta(days=days)

    with connection() as db:
        rows = db.execute(
            "SELECT * FROM alarm_events WHERE started_at>=? ORDER BY started_at",
            (start.isoformat().replace("+00:00", "Z"),),
        ).fetchall()
        events = [dict(row) for row in rows]

    type_counter: Counter[str] = Counter()
    station_counter: Counter[str] = Counter()
    weekday_counter: Counter[int] = Counter({index: 0 for index in range(7)})
    hour_counter: Counter[int] = Counter({hour: 0 for hour in range(24)})
    first_delays: list[float] = []
    complete_delays: list[float] = []
    complete_count = 0
    followup_events = 0
    followup_total = 0

    for event in events:
        started = _parse(event.get("started_at"))
        first = _parse(event.get("first_delivered_at"))
        completed = _parse(event.get("completed_at"))
        if event.get("alarm_type"):
            type_counter[str(event["alarm_type"])] += 1
        if event.get("station"):
            station_counter[str(event["station"])] += 1
        if started:
            local = started.astimezone()
            weekday_counter[local.weekday()] += 1
            hour_counter[local.hour] += 1
        if started and first:
            first_delays.append(max(0.0, (first - started).total_seconds()))
        if started and completed:
            complete_delays.append(max(0.0, (completed - started).total_seconds()))
        if event.get("status") == "complete":
            complete_count += 1
        followups = int(event.get("followup_count") or 0)
        if followups:
            followup_events += 1
        followup_total += followups

    today = local_now.date()
    dates = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
    daily_counter = Counter({day: 0 for day in dates})
    daily_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
    with connection() as db:
        daily_rows = db.execute(
            "SELECT started_at FROM alarm_events WHERE started_at>=?",
            (daily_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),),
        ).fetchall()
    for row in daily_rows:
        started = _parse(str(row["started_at"]))
        if not started:
            continue
        day = started.astimezone().date()
        if day in daily_counter:
            daily_counter[day] += 1

    weekdays = [
        {"name": WEEKDAYS_DA[index], "count": weekday_counter[index]}
        for index in range(7)
    ]
    hours = [
        {"name": f"{hour:02d}–{(hour + 1) % 24:02d}", "count": hour_counter[hour]}
        for hour in range(24)
    ]
    daily = [
        {"date": day.strftime("%d/%m"), "count": daily_counter[day]}
        for day in dates
    ]

    peak_weekday = max(weekdays, key=lambda row: row["count"]) if events else None
    peak_hour = max(hours, key=lambda row: row["count"]) if events else None

    return {
        "days": days,
        "total": len(events),
        "complete": complete_count,
        "complete_pct": _pct(complete_count, len(events)),
        "followup_events": followup_events,
        "followup_total": followup_total,
        "followup_pct": _pct(followup_events, len(events)),
        "first": _duration(first_delays),
        "complete_time": _duration(complete_delays),
        "daily": daily,
        "weekdays": weekdays,
        "hours": hours,
        "types": type_counter.most_common(10),
        "stations": station_counter.most_common(10),
        "peak_weekday": peak_weekday,
        "peak_hour": peak_hour,
    }
