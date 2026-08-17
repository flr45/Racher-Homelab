from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_MAX_FILTER_TERMS = 100
_MAX_TERM_LENGTH = 80
_SPLIT_RE = re.compile(r"[\n,;]+")


def normalize_filter_terms(values: Any) -> list[str]:
    """Normalize admin supplied words/phrases while preserving readable spelling."""
    if isinstance(values, str):
        candidates: Iterable[Any] = _SPLIT_RE.split(values)
    elif isinstance(values, (list, tuple, set)):
        candidates = values
    else:
        candidates = []

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = re.sub(r"\s+", " ", str(candidate or "")).strip()
        if not term:
            continue
        if len(term) > _MAX_TERM_LENGTH:
            raise ValueError(f"Et filter må højst være {_MAX_TERM_LENGTH} tegn.")
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(term)
        if len(result) > _MAX_FILTER_TERMS:
            raise ValueError(f"Der kan højst gemmes {_MAX_FILTER_TERMS} alarmfiltre.")
    return result


def match_filter_term(message: str, terms: Iterable[str]) -> str | None:
    """Return the first case-insensitive word/phrase contained in the alarm text."""
    haystack = str(message or "").casefold()
    for term in terms:
        needle = str(term or "").strip()
        if needle and needle.casefold() in haystack:
            return needle
    return None


def alarm_clock(received_at: str | None) -> str:
    """Return the alarm wall-clock time, preserving timezone-naive PDW timestamps."""
    raw = str(received_at or "").strip()
    if not raw:
        moment = datetime.now(timezone.utc)
    else:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return "—"

    # PDL/PDW writes its local wall-clock time without a timezone. Do not shift it.
    if moment.tzinfo is None:
        return moment.strftime("%H:%M:%S")

    zone_name = os.getenv("PAGER_TIMEZONE", "Europe/Copenhagen")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return moment.astimezone(zone).strftime("%H:%M:%S")


class AlarmFilterStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_filter_terms (
                    term TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at TEXT NOT NULL,
                    created_by INTEGER,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                )
                """
            )

    def list_terms(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT term FROM alarm_filter_terms ORDER BY term COLLATE NOCASE"
            ).fetchall()
        return [str(row["term"]) for row in rows]

    def replace_terms(self, values: Any, user_id: int | None = None) -> list[str]:
        terms = normalize_filter_terms(values)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("DELETE FROM alarm_filter_terms")
            conn.executemany(
                "INSERT INTO alarm_filter_terms(term, created_at, created_by) VALUES (?, ?, ?)",
                [(term, now, user_id) for term in terms],
            )
        return terms

    def match(self, message: str) -> str | None:
        return match_filter_term(message, self.list_terms())


def _timed_event(core: Any, event: dict[str, Any]) -> dict[str, Any]:
    decorated = dict(event)
    clock = alarm_clock(str(event.get("received_at") or ""))
    message = core.public_message(str(event.get("message") or ""))
    decorated["message"] = f"Alarmtid {clock}\n{message}" if clock != "—" else message
    return decorated


def install_alarm_rules(core: Any) -> AlarmFilterStore:
    """Install global manual pager filters and add alarm time to notifications.

    A matching pager alarm is retained in admin history with raw data intact, but
    is marked delivery-ineligible before Pushover/Web Push/routing can run.
    """
    store = AlarmFilterStore(core.DB_PATH)

    @core.app.get("/api/alarm-filters")
    @core.auth_required(admin=True)
    def api_alarm_filters_get():
        return core.jsonify({"terms": store.list_terms()})

    @core.app.put("/api/alarm-filters")
    @core.auth_required(admin=True)
    def api_alarm_filters_put():
        payload = core.request.get_json(silent=True) or {}
        try:
            terms = store.replace_terms(payload.get("terms", []), int(core.g.user["id"]))
        except ValueError as exc:
            return core.jsonify({"ok": False, "error": str(exc)}), 400
        core.storage.add_audit(
            core.g.user["id"],
            "alarm-filter-update",
            f"filters={len(terms)}",
        )
        return core.jsonify({"ok": True, "terms": terms})

    original_ingest = core.ingest_event

    def filtered_ingest(event: Any) -> int:
        message = core.public_message(str(getattr(event, "message", "") or ""))
        matched = store.match(message)
        if not matched:
            return original_ingest(event)

        data = event.to_dict()
        data["message"] = message
        data.update({
            "message_fingerprint": core.adaptive.exact_signature(message),
            "relevance_class": "filtered",
            "relevance_score": 0.0,
            "suppressed_reason": f"word-filter:{matched}",
            "duplicate_of": None,
            "delivery_eligible": False,
            "decision_reason": f"manuelt ordfilter matchede '{matched}'; råmeldingen er gemt, levering undertrykt",
        })
        station, routing_source = core.routing.classify(
            data.get("ric"), data.get("station"), message
        )
        data["station"] = station
        data["routing_source"] = routing_source
        message_id = core.storage.add_message(data)
        core.adaptive.observe(message_id, message)
        return message_id

    core.ingest_event = filtered_ingest

    original_pushover = core.maybe_notify_pushover
    original_web_push = core.send_web_push_for_event

    def timed_pushover(message_id: int, event: dict[str, Any]) -> None:
        return original_pushover(message_id, _timed_event(core, event))

    def timed_web_push(message_id: int, event: dict[str, Any]) -> None:
        return original_web_push(message_id, _timed_event(core, event))

    core.maybe_notify_pushover = timed_pushover
    core.send_web_push_for_event = timed_web_push
    return store
