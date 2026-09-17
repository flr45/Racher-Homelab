from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from storage import connection, utcnow_iso

EVENT_LINK_MINUTES = 120


def ensure_station_event_schema() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS station_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                aliases TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recipient_station_filters (
                recipient_id INTEGER NOT NULL,
                station_name TEXT NOT NULL,
                PRIMARY KEY(recipient_id, station_name),
                FOREIGN KEY(recipient_id) REFERENCES recipients(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS admin_stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recipient_admin_station (
                recipient_id INTEGER PRIMARY KEY,
                station_id INTEGER NOT NULL,
                FOREIGN KEY(recipient_id) REFERENCES recipients(id) ON DELETE CASCADE,
                FOREIGN KEY(station_id) REFERENCES admin_stations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alarm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                sender TEXT NOT NULL,
                station TEXT,
                alarm_type TEXT,
                address TEXT,
                status TEXT NOT NULL DEFAULT 'waiting',
                started_at TEXT NOT NULL,
                first_delivered_at TEXT,
                completed_at TEXT,
                last_update_at TEXT NOT NULL,
                followup_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS alarm_event_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                inbound_id INTEGER NOT NULL UNIQUE,
                group_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                raw_body TEXT NOT NULL,
                part_current INTEGER,
                part_total INTEGER,
                received_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES alarm_events(id) ON DELETE CASCADE,
                FOREIGN KEY(inbound_id) REFERENCES inbound_messages(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_alarm_events_started
                ON alarm_events(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alarm_events_sender
                ON alarm_events(sender, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alarm_messages_event
                ON alarm_event_messages(event_id, received_at);
            """
        )


def _clean(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def list_station_rules() -> list[dict]:
    ensure_station_event_schema()
    with connection() as db:
        rows = db.execute(
            "SELECT id, name, aliases, active, created_at FROM station_rules ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(row) for row in rows]


def save_station_rule(name: str, aliases: str = "") -> int:
    ensure_station_event_schema()
    cleaned_name = _clean(name)
    if not cleaned_name:
        raise ValueError("Stationsnavn mangler")
    alias_values = [
        _clean(value)
        for value in re.split(r"[,;\n]+", aliases or "")
        if _clean(value)
    ]
    normalized_aliases = ", ".join(dict.fromkeys(alias_values))
    with connection() as db:
        cursor = db.execute(
            "INSERT INTO station_rules(name, aliases, active, created_at) VALUES(?,?,1,?)",
            (cleaned_name, normalized_aliases, utcnow_iso()),
        )
        return int(cursor.lastrowid)


def set_station_rule_active(station_id: int, active: bool) -> None:
    ensure_station_event_schema()
    with connection() as db:
        db.execute(
            "UPDATE station_rules SET active=? WHERE id=?",
            (1 if active else 0, station_id),
        )


def delete_station_rule(station_id: int) -> None:
    ensure_station_event_schema()
    with connection() as db:
        row = db.execute("SELECT name FROM station_rules WHERE id=?", (station_id,)).fetchone()
        if row:
            db.execute(
                "DELETE FROM recipient_station_filters WHERE station_name=?",
                (str(row["name"]),),
            )
        db.execute("DELETE FROM station_rules WHERE id=?", (station_id,))


def resolve_station(body: str) -> str | None:
    ensure_station_event_schema()
    text = _clean(body)
    folded = text.casefold()

    with connection() as db:
        rules = db.execute(
            "SELECT name, aliases FROM station_rules WHERE active=1 ORDER BY LENGTH(name) DESC"
        ).fetchall()

    for row in rules:
        candidates = [str(row["name"])]
        candidates.extend(
            value.strip()
            for value in str(row["aliases"] or "").split(",")
            if value.strip()
        )
        for candidate in candidates:
            token = candidate.casefold()
            if token and (
                f"({token})" in folded
                or re.search(rf"(?<!\w){re.escape(token)}(?!\w)", folded)
            ):
                return str(row["name"])

    station_match = re.search(r"\(([A-ZÆØÅ0-9-]{1,12})\)", text, re.IGNORECASE)
    return station_match.group(1).upper() if station_match else None


def infer_kind(body: str) -> str:
    raw = body or ""
    compact = _clean(raw).casefold()
    if compact.startswith("test") and "m+v" not in compact and "·" not in raw and len(compact) < 80:
        return "test"
    if "sending 2" in compact:
        return "sending2_prealert" if "afventer resten" in compact else "sending2_complete"
    if "alarm modtaget" in compact and "afventer resten" in compact:
        return "alarm_prealert"
    return "alarm_complete"


def extract_event_fields(raw_body: str) -> tuple[str | None, str | None, str | None]:
    text = (raw_body or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = " ".join(lines)
    station = resolve_station(joined)

    alarm_type = None
    segments = [part.strip() for part in re.split(r"\s*·\s*", joined) if part.strip()]
    if len(segments) >= 2:
        candidate = segments[1]
        if not re.search(r"\bsending\s*2\b", candidate, re.IGNORECASE):
            alarm_type = candidate[:160]

    address = None
    candidates = lines + segments
    street_pattern = re.compile(
        r"\b[\wÆØÅæøå.'-]+(?:\s+[\wÆØÅæøå.'-]+){0,4}(?:vej|gade|all[eé]|stræde|vænget|boulevard|torv|plads|stien|parken)\s+\d{1,4}[A-Za-z]?\b",
        re.IGNORECASE,
    )
    for candidate in candidates:
        if re.search(r"\b\d{4}\b", candidate) and re.search(r"\d", candidate):
            address = candidate[:255]
            break
        if street_pattern.search(candidate):
            address = candidate[:255]
            break

    return station, alarm_type, address


def _recent_event(sender: str, received_at: str, *, waiting_only: bool = False) -> dict | None:
    received = _parse_iso(received_at)
    lower = _iso(received - timedelta(minutes=EVENT_LINK_MINUTES))
    upper = _iso(received + timedelta(minutes=5))
    sql = (
        "SELECT * FROM alarm_events WHERE sender=? AND started_at>=? AND started_at<=?"
    )
    params: list[object] = [sender, lower, upper]
    if waiting_only:
        sql += " AND status='waiting'"
    sql += " ORDER BY started_at DESC LIMIT 1"
    with connection() as db:
        row = db.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


def record_alarm_message(
    inbound_id: int,
    *,
    sender: str,
    body: str,
    received_at: str,
) -> tuple[int | None, str | None, str]:
    """Group one accepted SMS into an alarm event.

    Returns (event_id, station, kind). Test messages are intentionally not
    converted into alarm events.
    """
    ensure_station_event_schema()
    kind = infer_kind(body)
    if kind == "test":
        return None, resolve_station(body), kind

    with connection() as db:
        existing = db.execute(
            "SELECT event_id FROM alarm_event_messages WHERE inbound_id=?",
            (inbound_id,),
        ).fetchone()
        if existing:
            event = db.execute(
                "SELECT id, station FROM alarm_events WHERE id=?",
                (int(existing["event_id"]),),
            ).fetchone()
            return int(event["id"]), event["station"], kind

    station, alarm_type, address = extract_event_fields(body)
    is_followup = kind.startswith("sending2")

    if is_followup:
        event = _recent_event(sender, received_at)
    elif kind == "alarm_complete":
        event = _recent_event(sender, received_at, waiting_only=True)
    else:
        event = None

    now = utcnow_iso()
    with connection() as db:
        if event is None:
            digest = hashlib.sha256(
                f"{sender}|{received_at}|{body}|{inbound_id}".encode("utf-8")
            ).hexdigest()
            event_key = f"win:{digest}"[:128]
            status = "waiting" if kind == "alarm_prealert" else "complete"
            completed_at = None if status == "waiting" else now
            cursor = db.execute(
                """
                INSERT INTO alarm_events(
                    event_key, sender, station, alarm_type, address, status,
                    started_at, first_delivered_at, completed_at,
                    last_update_at, followup_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    event_key,
                    sender,
                    station,
                    alarm_type,
                    address,
                    status,
                    received_at,
                    None,
                    completed_at,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
        else:
            event_id = int(event["id"])

        group_key = f"{kind}:{inbound_id}"
        db.execute(
            """
            INSERT OR IGNORE INTO alarm_event_messages(
                event_id, inbound_id, group_key, kind, raw_body,
                part_current, part_total, received_at, created_at
            ) VALUES(?,?,?,?,?,NULL,NULL,?,?)
            """,
            (event_id, inbound_id, group_key, kind, body, received_at, now),
        )

        current = db.execute(
            "SELECT station, alarm_type, address, followup_count FROM alarm_events WHERE id=?",
            (event_id,),
        ).fetchone()
        updates = {
            "station": station or current["station"],
            "alarm_type": alarm_type or current["alarm_type"],
            "address": address or current["address"],
        }
        followup_count = int(current["followup_count"] or 0)
        if is_followup:
            followup_count += 1
        status = "complete" if kind in {"alarm_complete", "sending2_complete"} else None
        db.execute(
            """
            UPDATE alarm_events
            SET station=?, alarm_type=?, address=?,
                status=COALESCE(?, status),
                completed_at=CASE WHEN ?='complete' THEN COALESCE(completed_at, ?) ELSE completed_at END,
                last_update_at=?, followup_count=?
            WHERE id=?
            """,
            (
                updates["station"],
                updates["alarm_type"],
                updates["address"],
                status,
                status,
                now,
                now,
                followup_count,
                event_id,
            ),
        )

    return event_id, station, kind


def mark_first_delivery(inbound_id: int, delivered_at: str | None = None) -> None:
    ensure_station_event_schema()
    when = delivered_at or utcnow_iso()
    with connection() as db:
        row = db.execute(
            "SELECT event_id FROM alarm_event_messages WHERE inbound_id=?",
            (inbound_id,),
        ).fetchone()
        if not row:
            return
        db.execute(
            """
            UPDATE alarm_events
            SET first_delivered_at=COALESCE(first_delivered_at, ?), last_update_at=?
            WHERE id=?
            """,
            (when, when, int(row["event_id"])),
        )


def recipient_filters(recipient_id: int) -> list[str]:
    ensure_station_event_schema()
    with connection() as db:
        rows = db.execute(
            "SELECT station_name FROM recipient_station_filters WHERE recipient_id=? ORDER BY station_name COLLATE NOCASE",
            (recipient_id,),
        ).fetchall()
        return [str(row["station_name"]) for row in rows]


def set_recipient_filters(recipient_id: int, stations: list[str]) -> None:
    ensure_station_event_schema()
    cleaned = sorted({_clean(value) for value in stations if _clean(value)}, key=str.casefold)
    with connection() as db:
        db.execute("DELETE FROM recipient_station_filters WHERE recipient_id=?", (recipient_id,))
        for station in cleaned:
            db.execute(
                "INSERT INTO recipient_station_filters(recipient_id, station_name) VALUES(?,?)",
                (recipient_id, station),
            )


def active_recipients_for_station(station: str | None) -> list[dict]:
    ensure_station_event_schema()
    with connection() as db:
        recipients = [
            dict(row)
            for row in db.execute(
                "SELECT id, name, phone, active, created_at FROM recipients WHERE active=1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
        ]
        for recipient in recipients:
            rows = db.execute(
                "SELECT station_name FROM recipient_station_filters WHERE recipient_id=?",
                (int(recipient["id"]),),
            ).fetchall()
            filters = {str(row["station_name"]).casefold() for row in rows}
            recipient["station_filters"] = sorted(filters)

    if not recipients:
        return []
    output = []
    station_key = (station or "").casefold()
    for recipient in recipients:
        filters = set(recipient.pop("station_filters", []))
        if not filters or (station_key and station_key in filters):
            output.append(recipient)
    return output


def station_for_inbound(inbound_id: int) -> str | None:
    ensure_station_event_schema()
    with connection() as db:
        row = db.execute(
            """
            SELECT e.station
            FROM alarm_event_messages m
            JOIN alarm_events e ON e.id=m.event_id
            WHERE m.inbound_id=?
            """,
            (inbound_id,),
        ).fetchone()
        return str(row["station"]) if row and row["station"] else None


def recent_alarm_events(limit: int = 100) -> list[dict]:
    ensure_station_event_schema()
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM alarm_events ORDER BY started_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]


def alarm_event_timeline(event_id: int) -> list[dict]:
    ensure_station_event_schema()
    with connection() as db:
        rows = db.execute(
            """
            SELECT m.*, i.sender, i.processing_status,
                   COALESCE(SUM(CASE WHEN d.status='sent' THEN 1 ELSE 0 END),0) AS sent_count,
                   COALESCE(SUM(CASE WHEN d.status='failed' THEN 1 ELSE 0 END),0) AS failed_count
            FROM alarm_event_messages m
            JOIN inbound_messages i ON i.id=m.inbound_id
            LEFT JOIN whatsapp_deliveries d ON d.inbound_id=i.id
            WHERE m.event_id=?
            GROUP BY m.id
            ORDER BY m.received_at, m.id
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def stats_snapshot() -> dict:
    ensure_station_event_schema()
    now = datetime.now(timezone.utc)
    start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start_7 = now - timedelta(days=7)
    start_30 = now - timedelta(days=30)

    events = recent_alarm_events(5000)
    today = days7 = days30 = waiting = followups = 0
    first_delays: list[float] = []
    complete_delays: list[float] = []
    type_counter: Counter[str] = Counter()
    hour_counter: Counter[int] = Counter()

    for event in events:
        started = _parse_iso(str(event["started_at"]))
        if started >= start_today:
            today += 1
        if started >= start_7:
            days7 += 1
        if started >= start_30:
            days30 += 1
            followups += int(event["followup_count"] or 0)
            if event["alarm_type"]:
                type_counter[str(event["alarm_type"])] += 1
            hour_counter[started.astimezone().hour] += 1
            if event["first_delivered_at"]:
                first = _parse_iso(str(event["first_delivered_at"]))
                first_delays.append(max(0.0, (first - started).total_seconds()))
            if event["completed_at"]:
                completed = _parse_iso(str(event["completed_at"]))
                complete_delays.append(max(0.0, (completed - started).total_seconds()))
        if str(event["status"]) == "waiting":
            waiting += 1

    return {
        "today": today,
        "days7": days7,
        "days30": days30,
        "waiting": waiting,
        "followups": followups,
        "avg_first": round(sum(first_delays) / len(first_delays), 1) if first_delays else None,
        "avg_complete": round(sum(complete_delays) / len(complete_delays), 1) if complete_delays else None,
        "top_types": type_counter.most_common(8),
        "hours": sorted(hour_counter.items()),
    }


def list_admin_stations() -> list[dict]:
    ensure_station_event_schema()
    with connection() as db:
        rows = db.execute(
            "SELECT id, name, created_at FROM admin_stations ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(row) for row in rows]


def save_admin_station(name: str) -> int:
    ensure_station_event_schema()
    cleaned = _clean(name)
    if not cleaned:
        raise ValueError("Stationsnavn mangler")
    with connection() as db:
        cursor = db.execute(
            "INSERT INTO admin_stations(name, created_at) VALUES(?,?)",
            (cleaned, utcnow_iso()),
        )
        return int(cursor.lastrowid)


def delete_admin_station(station_id: int) -> None:
    ensure_station_event_schema()
    with connection() as db:
        db.execute("DELETE FROM admin_stations WHERE id=?", (station_id,))


def set_recipient_admin_station(recipient_id: int, station_id: int | None) -> None:
    ensure_station_event_schema()
    with connection() as db:
        db.execute("DELETE FROM recipient_admin_station WHERE recipient_id=?", (recipient_id,))
        if station_id is not None:
            db.execute(
                "INSERT INTO recipient_admin_station(recipient_id, station_id) VALUES(?,?)",
                (recipient_id, station_id),
            )


def recipient_admin_station(recipient_id: int) -> int | None:
    ensure_station_event_schema()
    with connection() as db:
        row = db.execute(
            "SELECT station_id FROM recipient_admin_station WHERE recipient_id=?",
            (recipient_id,),
        ).fetchone()
        return int(row["station_id"]) if row else None
