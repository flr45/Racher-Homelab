"""Background alarm geocoding using KDS Adressevælger, with legacy provider support.

Delivery completes independently. Only unique exact/near-exact address matches
are persisted; uncertain matches remain available for manual placement.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pyproj import Transformer
from pyproj.exceptions import ProjError
import urllib.error
import urllib.parse
import urllib.request

from flask import request

import events_app as events
import history_app as history
import station_names_app as previous

app = previous.app
db = history.db
base = history.base
log = logging.getLogger("sbr-pager-geocode")

AUTO_GEOCODE = os.getenv("SBR_PAGER_AUTO_GEOCODE", "true").lower() == "true"
GEOCODER_URL = os.getenv(
    "SBR_PAGER_GEOCODER_URL",
    "https://adressevaelger.dk/vask/",
).strip()
GEOCODER_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("SBR_PAGER_GEOCODER_TIMEOUT_SECONDS", "4")),
)

GEOCODER_TOKEN = os.getenv("SBR_PAGER_GEOCODER_TOKEN", "adressevaelger123").strip()
_UTM_TO_WGS84 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def _valid_coordinates(value) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        longitude = float(value[0])
        latitude = float(value[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return latitude, longitude


def _find_adgangspunkt_coordinates(value) -> tuple[float, float] | None:
    if isinstance(value, dict):
        point = value.get("adgangspunkt")
        if isinstance(point, dict):
            parsed = _valid_coordinates(point.get("koordinater"))
            if parsed:
                return parsed
        for child in value.values():
            parsed = _find_adgangspunkt_coordinates(child)
            if parsed:
                return parsed
    elif isinstance(value, list):
        for child in value:
            parsed = _find_adgangspunkt_coordinates(child)
            if parsed:
                return parsed
    return None


def parse_geocode_result(document: dict) -> tuple[float, float, str] | None:
    """Return (lat, lon, match category) for safe DAWA datavask results."""
    if not isinstance(document, dict):
        return None
    category = str(document.get("kategori") or "").strip().upper()
    if category not in {"A", "B"}:
        return None
    rows = document.get("resultater")
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0] if isinstance(rows[0], dict) else {}
    current = row.get("aktueladresse") or row.get("adresse") or row
    coordinates = _find_adgangspunkt_coordinates(current)
    if not coordinates:
        return None
    return coordinates[0], coordinates[1], category


def parse_adressevaelger_location(document):
    if not isinstance(document, dict) or document.get("status") != "ok":
        return None
    address = document.get("adresse") or {}
    house = address.get("husnummer") or document.get("husnummer") or {}
    if str(house.get("status")) != "3":
        return None
    geometry = (house.get("adgangspunkt") or {}).get("geometri") or {}
    crs = ((geometry.get("crs") or {}).get("properties") or {}).get("name")
    coords = geometry.get("coordinates")
    if geometry.get("type") != "Point" or crs != "EPSG:25832" or not isinstance(coords, list) or len(coords) != 2:
        return None
    try:
        lon, lat = _UTM_TO_WGS84.transform(float(coords[0]), float(coords[1]), errcheck=True)
    except (TypeError, ValueError, ProjError):
        return None
    return _valid_coordinates([lon, lat])


def _request_json(url):
    outgoing = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "SBR-Pager/1.0 automatic-alarm-map"})
    with urllib.request.urlopen(outgoing, timeout=GEOCODER_TIMEOUT_SECONDS) as response:
        return json.loads(response.read(1048576).decode("utf-8"))


def address_candidates(address: str) -> list[str]:
    """Prefer the address-only segment if an alarm line contains · separators."""
    raw = " ".join((address or "").strip().split())
    if not raw:
        return []

    candidates: list[str] = []
    if "·" in raw:
        pieces = [" ".join(piece.strip().split()) for piece in raw.split("·")]
        # Addresses are normally the last alarm segment. Prefer specific pieces
        # containing a house number before trying the complete stored value.
        for piece in reversed(pieces):
            if re.search(r"\d", piece):
                candidates.append(piece)
    candidates.append(raw)

    unique: list[str] = []
    for value in candidates:
        if value and value not in unique:
            unique.append(value)
    return unique


def geocode_address(address: str):
    """Use the official replacement API, retaining configured DAWA-compatible URLs."""
    if not AUTO_GEOCODE or not GEOCODER_URL:
        return None
    official = urllib.parse.urlsplit(GEOCODER_URL).hostname == "adressevaelger.dk"
    for candidate in address_candidates(address):
        try:
            params = {"adresse": candidate, "token": GEOCODER_TOKEN} if official else {"betegnelse": candidate}
            document = _request_json(GEOCODER_URL + ("&" if "?" in GEOCODER_URL else "?") + urllib.parse.urlencode(params))
            if official:
                if not isinstance(document, dict):
                    return None
                code = (document.get("vaskestatus") or {}).get("kode")
                match = document.get("vaskeresultat") or {}
                # Intervals and ambiguous/no matches must never become guessed alarm pins.
                if code not in (1000, 900) or str(match.get("status")) != "3":
                    continue
                address_id = str(uuid.UUID(match.get("adresse_id_lokalid", "")))
                root = urllib.parse.urlsplit(GEOCODER_URL)
                detail_url = urllib.parse.urlunsplit((root.scheme, root.netloc, "/adresser/" + address_id, urllib.parse.urlencode({"token": GEOCODER_TOKEN}), ""))
                coords = parse_adressevaelger_location(_request_json(detail_url))
                parsed = (coords[0], coords[1], str(code)) if coords else None
            else:
                parsed = parse_geocode_result(document)
                if isinstance(document, dict) and document.get("kategori") == "C":
                    return None
            if parsed:
                return {"latitude": parsed[0], "longitude": parsed[1], "category": parsed[2], "query": candidate}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError) as exc:
            # Exception URLs can contain API tokens: only log the exception class.
            log.warning("Automatisk adresseopslag fejlede (%s)", type(exc).__name__)
            return None
    return None


def geocode_event(event_id: int) -> bool:
    """Geocode and persist one event if it has an address and no location yet."""
    if not AUTO_GEOCODE:
        return False

    with app.app_context():
        event = db.session.get(events.AlarmEvent, event_id)
        if event is None or not (event.address or "").strip():
            return False
        if history.event_location(event.id) is not None:
            return False
        address = event.address

    result = geocode_address(address)
    if result is None:
        return False

    with app.app_context():
        event = db.session.get(events.AlarmEvent, event_id)
        if event is None or history.event_location(event.id) is not None:
            return False
        db.session.add(
            history.AlarmEventLocation(
                event_id=event.id,
                latitude=result["latitude"],
                longitude=result["longitude"],
            )
        )
        db.session.commit()
        log.info(
            "Alarm #%s lagt automatisk på kortet via adresse-match %s: %s",
            event.id,
            result["category"],
            result["query"],
        )
        return True


def schedule_event_geocode(event_id: int) -> None:
    """Run geocoding outside the alarm ingest response path."""
    if not AUTO_GEOCODE:
        return
    worker = threading.Thread(
        target=geocode_event,
        args=(event_id,),
        name=f"sbr-geocode-{event_id}",
        daemon=True,
    )
    worker.start()


def backfill_missing_locations(limit: int = 100) -> dict:
    """One-off helper for existing alarm events with an address but no map pin."""
    with app.app_context():
        ids = [
            row.id
            for row in events.AlarmEvent.query.filter(events.AlarmEvent.address.is_not(None))
            .order_by(events.AlarmEvent.started_at.desc())
            .limit(max(1, int(limit)))
            .all()
            if history.event_location(row.id) is None
        ]

    added = 0
    for event_id in ids:
        if geocode_event(event_id):
            added += 1
    return {"checked": len(ids), "added": added}


_original_incoming = app.view_functions["incoming"]


def incoming_with_auto_geocode():
    payload = request.get_json(silent=True) or {}
    response = _original_incoming()
    try:
        status = response[1] if isinstance(response, tuple) else getattr(response, "status_code", 200)
        if 200 <= int(status) < 300:
            sender = base.normalize_phone(payload.get("sender", ""))
            body = str(payload.get("body", "")).strip()
            received_at = base.parse_received_at(payload.get("receivedAt"))
            source_id = base.make_source_id(sender, body, received_at, payload.get("sourceMessageId"))
            inbound = base.InboundMessage.query.filter_by(source_id=source_id).first()
            if inbound is not None and inbound.accepted:
                event_message = events.AlarmEventMessage.query.filter_by(inbound_id=inbound.id).first()
                if event_message is not None:
                    event = db.session.get(events.AlarmEvent, event_message.event_id)
                    if event is not None and event.address and history.event_location(event.id) is None:
                        schedule_event_geocode(event.id)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("Kunne ikke planlægge automatisk kortplacering; alarmen er allerede behandlet")
    return response


app.view_functions["incoming"] = incoming_with_auto_geocode
