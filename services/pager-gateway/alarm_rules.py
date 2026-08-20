from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_MAX_FILTER_TERMS = 100
_MAX_TERM_LENGTH = 80
_SPLIT_RE = re.compile(r"[\n,;]+")
_ALARM_HINT_RE = re.compile(r"(?:\b(?:BRAND(?:ALARM)?|ALARM|ISL|VSBV|ØF|VCT)\b|M\+S)", re.I)
_POSTAL_LOCALITY_RE = re.compile(r"\b(?P<postcode>\d{4})\s+(?P<locality>[A-Za-zÆØÅæøå][A-Za-zÆØÅæøå-]{2,})\b")
# Known 1200-baud dispatch families are not limited to the historic "NR RI(...)M+S"
# spelling. Næstved traffic has also been observed as e.g. "MN NÆ(1+5)M+S".
# Keep the old function names for compatibility, but recognise both families.
_NR_BURST_RE = re.compile(
    r"(?:\bNR\b.{0,28}?M\+S\b|\b[A-ZÆØÅ]{1,4}\s+[A-ZÆØÅ]{1,4}\([^)]{1,16}\)M\+S\b)",
    re.I,
)


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


def _clean_pager_message(message: str) -> str:
    """Preserve pager payload prefixes; only trim surrounding whitespace.

    Prefixes such as ``@8`` and ``$9`` are present in the known-good pager feed
    and are therefore treated as real payload rather than decoder garbage.
    """
    return str(message or "").strip()


def _looks_nr_dispatch(message: str) -> bool:
    """Recognise a known M+S dispatch family even when a few prefix bits are corrupt."""
    return bool(_NR_BURST_RE.search(str(message or "")))


def _quality_noise_reason(message: str) -> str | None:
    """Catch tiny/broken POCSAG alpha fragments that are useless as notifications."""
    value = str(message or "").strip()
    if not value:
        return None

    # A recurring failure mode is a short first/second copy of an M+S dispatch,
    # e.g. "NR RI(1+5)M+S · Ringsted Svlmv2v" or the observed Næstved copy
    # "MN NÆ(1+5)M+S · Park%y4gs9". Do not let that partial copy become the
    # notification that blocks a complete copy a few seconds later.
    if (
        _looks_nr_dispatch(value)
        and len(value) <= 44
        and not re.search(r"\b(?:BRAND(?:ALARM)?|ALARM)\b|\b\d{4}\b", value, re.I)
    ):
        return "decoder-partial"

    if _ALARM_HINT_RE.search(value):
        return None

    compact = "".join(char for char in value if not char.isspace())
    if len(value) <= 12 and compact:
        symbols = sum(1 for char in compact if not char.isalnum() and char not in "ÆØÅæøå")
        if "?" in compact or symbols / len(compact) >= 0.25:
            return "decoder-fragment"
    return None


def _normalized_incident_text(message: str) -> str:
    value = str(message or "").casefold().replace("?", "")
    value = re.sub(r"[^\wÆØÅæøå]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _incident_key(message: str) -> tuple[str, str] | None:
    """Return postcode/locality plus a long comparable incident tail."""
    normalized = _normalized_incident_text(message)
    match = _POSTAL_LOCALITY_RE.search(normalized)
    if not match:
        return None
    tail = normalized[match.start():].strip()
    if len(tail) < 28:
        return None
    return f"{match.group('postcode')} {match.group('locality').casefold()}", tail


def _parse_moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _find_extended_duplicate(store: "AlarmFilterStore", message: str, received_at: str) -> int | None:
    """Find the same dispatch burst or incident despite small decoder errors."""
    current = _parse_moment(received_at)
    if current is None:
        return None

    current_is_nr = _looks_nr_dispatch(message)
    current_incident = _incident_key(message)
    current_normalized = _normalized_incident_text(message)
    if not current_is_nr and current_incident is None:
        return None

    with store.connect() as conn:
        rows = conn.execute(
            """SELECT id, received_at, message, delivery_eligible, duplicate_of
               FROM messages
               ORDER BY id DESC LIMIT 120"""
        ).fetchall()

    for row in rows:
        previous = _parse_moment(row["received_at"])
        if previous is None:
            continue
        delta = (current - previous).total_seconds()
        if delta < 0 or delta > 90:
            continue

        previous_message = str(row["message"] or "")

        # One physical M+S dispatch is commonly repeated to several pager RICs
        # over only a few seconds. A single bad codeword can corrupt the place,
        # postcode or dispatch prefix, so exact locality matching is too brittle.
        # Compare the whole normalized payload inside this short burst window.
        if current_is_nr and delta <= 8 and _looks_nr_dispatch(previous_message):
            previous_normalized = _normalized_incident_text(previous_message)
            if current_normalized and previous_normalized:
                burst_similarity = SequenceMatcher(
                    None, current_normalized, previous_normalized, autojunk=False
                ).ratio()
                if burst_similarity >= 0.915:
                    return int(row["duplicate_of"] or row["id"])

        if current_incident is None:
            continue
        previous_incident = _incident_key(previous_message)
        if previous_incident is None:
            continue
        current_key, current_tail = current_incident
        previous_key, previous_tail = previous_incident
        if previous_key != current_key:
            continue

        shorter, longer = sorted((current_tail, previous_tail), key=len)
        containment = len(shorter) >= 28 and shorter in longer
        similarity = SequenceMatcher(None, current_tail, previous_tail, autojunk=False).ratio()
        if containment or similarity >= 0.88:
            return int(row["duplicate_of"] or row["id"])
    return None


class AlarmFilterStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_filter_terms (
                    term TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at TEXT NOT NULL,
                    created_by INTEGER
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
    """Install pager filtering, burst dedupe and notification timing."""
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

    def store_suppressed(
        event: Any,
        message: str,
        *,
        relevance_class: str,
        suppressed_reason: str,
        decision_reason: str,
        duplicate_of: int | None = None,
    ) -> int:
        data = event.to_dict()
        data["message"] = message
        data.update({
            "message_fingerprint": core.adaptive.exact_signature(message),
            "relevance_class": relevance_class,
            "relevance_score": 0.0 if relevance_class in {"noise", "filtered"} else 0.75,
            "suppressed_reason": suppressed_reason,
            "duplicate_of": duplicate_of,
            "delivery_eligible": False,
            "decision_reason": decision_reason,
        })
        station, routing_source = core.routing.classify(
            data.get("ric"), data.get("station"), message
        )
        data["station"] = station
        data["routing_source"] = routing_source
        message_id = core.storage.add_message(data)
        core.adaptive.observe(message_id, message)
        return message_id

    def filtered_ingest(event: Any) -> int:
        message = core.public_message(str(getattr(event, "message", "") or ""))
        message = core.public_message(_clean_pager_message(message))
        event.message = message

        quality_reason = _quality_noise_reason(message)
        if quality_reason:
            return store_suppressed(
                event,
                message,
                relevance_class="noise",
                suppressed_reason=quality_reason,
                decision_reason="pager-kvalitetsfilter: kort/ufuldstændigt decoder-fragment gemt uden notifikation",
            )

        matched = store.match(message)
        if matched:
            return store_suppressed(
                event,
                message,
                relevance_class="filtered",
                suppressed_reason=f"word-filter:{matched}",
                decision_reason=f"manuelt ordfilter matchede '{matched}'; råmeldingen er gemt, levering undertrykt",
            )

        duplicate_of = _find_extended_duplicate(store, message, str(getattr(event, "received_at", "") or ""))
        if duplicate_of is not None:
            return store_suppressed(
                event,
                message,
                relevance_class="unknown",
                suppressed_reason="duplicate",
                duplicate_of=duplicate_of,
                decision_reason=f"samme hændelse/radioburst gentaget; dublet af melding #{duplicate_of}",
            )

        return original_ingest(event)

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