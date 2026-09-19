"""Run the Huawei modem reader with direct SBR Pager fan-out.

Every ordinary inbound SMS is delivered to SBR Pager directly from the modem
worker before the existing SMS-gateway/Vagtbytte path runs. This keeps SBR
Pager independent of the legacy application flow while retaining the existing
SMS gateway for Vagtbytte, status commands and SMS forwarding.

Huawei may mark an unread SMS as read when it is returned by AT+CMGL=0. That
is unsafe for concatenated SMS: part 1 can become read before part 2 arrives,
so a later unread-only poll never sees the complete message. Therefore this
wrapper changes only the inbox listing command to AT+CMGL=4 (all stored SMS).
Completed messages are still deleted by the existing modem reader after normal
processing, while incomplete multipart groups stay on the SIM until complete.

For multipart alarm messages we wait a configurable grace period before sending
part 1 as a pre-alert. Normal short multipart alarms therefore usually arrive as
one complete WhatsApp message, while genuinely long/delayed multipart alarms
still produce an early warning. A later multipart "Sending 2" follows the same
rule.

Structured event metadata is sent together with each SBR Pager ingest so the
admin panel can group pre-alert, complete alarm and later Sending 2 updates into
one alarm event without changing the original alarm text.

Personal data is redacted in the SBR Pager payload before it leaves this modem
worker. The legacy SMS-gateway/Vagtbytte path still receives the original SMS.

Non-phone/alphanumeric senders are consumed without sending them to SBR Pager.
This prevents operator/service SMS messages from becoming permanently stuck on
the SIM and blocking or slowing later alarm messages.
"""

import hashlib
import json
import logging
import os
import re
import signal
import time
import urllib.error
import urllib.request

import modem_reader as reader
import privacy_filter
import sms_pdu

log = logging.getLogger("sms-modem-reader-sbr")

SBR_PAGER_INGEST_URL = os.getenv(
    "SBR_PAGER_INGEST_URL",
    "http://sms-whatsapp:8080/api/incoming",
).strip()
SBR_PAGER_INGEST_TOKEN = os.getenv("SBR_PAGER_INGEST_TOKEN", "").strip()
SBR_PAGER_PREALERT_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("SBR_PAGER_PREALERT_DELAY_SECONDS", "10")),
)
SBR_PAGER_PARENT_STATE_FILE = os.getenv(
    "SBR_PAGER_PARENT_STATE_FILE",
    "/data/sbr-pager-parent-events.json",
).strip()
SBR_PAGER_PARENT_MAX_AGE_SECONDS = max(
    300.0,
    float(os.getenv("SBR_PAGER_PARENT_MAX_AGE_SECONDS", "7200")),
)
SBR_PAGER_FOLLOWUP_WINDOW_SECONDS = max(
    1.0,
    float(os.getenv("SBR_PAGER_FOLLOWUP_WINDOW_SECONDS", "60")),
)
SBR_PAGER_IGNORE_COMMANDS = {
    " ".join(value.casefold().split())
    for value in os.getenv(
        "SBR_PAGER_IGNORE_COMMANDS",
        "status,server status,serverstatus",
    ).split(",")
    if value.strip()
}

_original_post_message = reader.post_message
_original_command = reader.command
_PHONE_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
_STATION_PATTERN = re.compile(r"\(([ASLKRB])\)", re.IGNORECASE)
_SENDING_2_PATTERN = re.compile(r"\bsending\s*2\b", re.IGNORECASE)
_multipart_first_seen: dict[str, float] = {}
_multipart_prealert_sent: set[str] = set()


def _read_parent_state() -> dict:
    if not SBR_PAGER_PARENT_STATE_FILE:
        return {}
    try:
        with open(SBR_PAGER_PARENT_STATE_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _write_parent_state(value: dict) -> None:
    if not SBR_PAGER_PARENT_STATE_FILE:
        return
    directory = os.path.dirname(SBR_PAGER_PARENT_STATE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = SBR_PAGER_PARENT_STATE_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp_path, SBR_PAGER_PARENT_STATE_FILE)


def detect_station_code(body: str | None) -> str | None:
    match = _STATION_PATTERN.search(body or "")
    return match.group(1).upper() if match else None


def remember_parent_event(sender: str, event_key: str, station: str | None = None) -> None:
    now = time.time()
    state = _read_parent_state()
    state = {
        key: row
        for key, row in state.items()
        if isinstance(row, dict)
        and now - float(row.get("updated_at", 0) or 0) <= SBR_PAGER_PARENT_MAX_AGE_SECONDS
    }
    state[sender] = {
        "event_key": event_key,
        "station": (station or "").upper() or None,
        "updated_at": now,
    }
    try:
        _write_parent_state(state)
    except OSError:
        log.exception("Kunne ikke gemme SBR Pager parent-event state")


def recent_parent_event(sender: str, max_age_seconds: float | None = None) -> dict | None:
    now = time.time()
    state = _read_parent_state()
    row = state.get(sender)
    if not isinstance(row, dict):
        return None
    try:
        age = now - float(row.get("updated_at", 0) or 0)
    except (TypeError, ValueError):
        return None
    max_age = (
        SBR_PAGER_PARENT_MAX_AGE_SECONDS
        if max_age_seconds is None
        else max(0.0, float(max_age_seconds))
    )
    if age < 0 or age > max_age:
        return None
    value = str(row.get("event_key") or "").strip()
    if not value:
        return None
    return {
        "event_key": value[:128],
        "station": str(row.get("station") or "").strip().upper() or None,
        "age_seconds": age,
    }


def recent_parent_event_key(sender: str) -> str | None:
    row = recent_parent_event(sender)
    return str(row["event_key"]) if row else None


def active_followup_parent_key(sender: str, body: str | None) -> str | None:
    # A new explicit station marker always starts a new incident, even if it
    # arrives inside the previous incident's follow-up window.
    if detect_station_code(body):
        return None
    row = recent_parent_event(sender, SBR_PAGER_FOLLOWUP_WINDOW_SECONDS)
    # Only explicit station alarms open the automatic follow-up window.
    if not row or not row.get("station"):
        return None
    return str(row["event_key"])


def normalized_command(body: str) -> str:
    return " ".join((body or "").strip().casefold().split())


def normalize_phone_sender(value: str) -> str | None:
    phone = re.sub(r"[\s().-]", "", value or "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not _PHONE_PATTERN.fullmatch(phone):
        return None
    return phone


def modem_command(port, value, timeout=8, expected="\r\nOK\r\n"):
    if value == "AT+CMGL=0":
        value = "AT+CMGL=4"
    return _original_command(port, value, timeout=timeout, expected=expected)


def multipart_group_key(
    sender: str,
    concat_reference: int | None,
    concat_total: int | None,
    timestamp: str,
) -> str:
    stable = "|".join(
        [
            reader.MODEM_DEVICE,
            sender,
            str(concat_reference),
            str(concat_total),
            timestamp,
        ]
    )
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return f"huawei-group:{digest}"


def post_sbr_payload(
    sender: str,
    body: str,
    received_at: str,
    source_message_id: str,
    *,
    event_key: str | None = None,
    group_key: str | None = None,
    message_kind: str | None = None,
    raw_body: str | None = None,
    part_current: int | None = None,
    part_total: int | None = None,
    parent_event_key: str | None = None,
):
    if not SBR_PAGER_INGEST_URL:
        return None
    if not SBR_PAGER_INGEST_TOKEN:
        raise RuntimeError("SBR_PAGER_INGEST_TOKEN mangler")

    safe_body = privacy_filter.redact_personal_data(body) or ""
    safe_raw_body = privacy_filter.redact_personal_data(raw_body)

    document = {
        "sender": sender,
        "body": safe_body,
        "receivedAt": received_at,
        "sourceMessageId": source_message_id,
    }
    optional = {
        "eventKey": event_key,
        "groupKey": group_key,
        "messageKind": message_kind,
        "rawBody": safe_raw_body,
        "partCurrent": part_current,
        "partTotal": part_total,
        "parentEventKey": parent_event_key,
    }
    document.update({key: value for key, value in optional.items() if value is not None})

    payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        SBR_PAGER_INGEST_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {SBR_PAGER_INGEST_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            response_body = response.read().decode("utf-8")
            if response.status not in {200, 201, 202}:
                raise RuntimeError(f"SBR Pager svarede HTTP {response.status}")
            return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"SBR Pager svarede HTTP {exc.code}: {details[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Kunne ikke kontakte SBR Pager: {exc.reason}") from exc


def complete_message_metadata(
    message: dict,
) -> tuple[str, str, str, int, int, str | None]:
    raw_body = (message.get("body") or "").strip()
    is_sending_2 = _SENDING_2_PATTERN.search(raw_body) is not None
    auto_parent_event_key = (
        None
        if is_sending_2
        else active_followup_parent_key(message["sender"], raw_body)
    )
    is_auto_followup = auto_parent_event_key is not None
    is_multipart = len(message.get("indices") or []) > 1
    source_id = reader.source_message_id(message)

    if is_multipart and message.get("_event_group_key"):
        group_key = str(message["_event_group_key"])
        total = int(message.get("_concat_total") or len(message.get("indices") or []))
    else:
        group_key = source_id
        total = 1

    if is_sending_2:
        kind = "sending2_complete"
        parent_event_key = recent_parent_event_key(message["sender"])
        heading = "📟 *SENDING 2 – KOMPLET*" if is_multipart else "📟 *SENDING 2*"
    elif is_auto_followup:
        kind = "followup_complete"
        parent_event_key = auto_parent_event_key
        heading = "📟 *OPFØLGNING – KOMPLET*" if is_multipart else "📟 *OPFØLGNING*"
    else:
        kind = "alarm_complete"
        parent_event_key = None
        heading = "🚨 *KOMPLET ALARM*" if is_multipart else ""

    formatted_body = f"{heading}\n{raw_body}" if heading else raw_body
    return formatted_body, raw_body, kind, total, total, parent_event_key


def post_to_sbr_pager(message: dict):
    (
        formatted_body,
        raw_body,
        kind,
        current,
        total,
        parent_event_key,
    ) = complete_message_metadata(message)
    source_id = reader.source_message_id(message)
    group_key = str(message.get("_event_group_key") or source_id)
    is_followup = kind.startswith(("sending2", "followup"))
    result = post_sbr_payload(
        sender=message["sender"],
        body=formatted_body,
        received_at=message["timestamp"],
        source_message_id=source_id,
        event_key=group_key,
        group_key=group_key,
        message_kind=kind,
        raw_body=raw_body,
        part_current=current,
        part_total=total,
        parent_event_key=parent_event_key,
    )
    station = detect_station_code(raw_body)
    if result is not None and result.get("accepted") and not is_followup and station:
        remember_parent_event(message["sender"], group_key, station)
    return result


def prealert_source_id(part: sms_pdu.DecodedSmsPart) -> str:
    stable = "|".join(
        [
            reader.MODEM_DEVICE,
            part.sender,
            str(part.concat_reference),
            str(part.concat_total),
            part.timestamp,
            part.text,
        ]
    )
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return f"huawei-prealert:{digest}"


def send_multipart_prealerts(parts: list[sms_pdu.DecodedSmsPart]) -> None:
    groups: dict[tuple[str, int, int], list[sms_pdu.DecodedSmsPart]] = {}

    for part in parts:
        if (
            part.concat_reference is None
            or part.concat_total is None
            or part.concat_part is None
        ):
            continue
        key = (part.sender, part.concat_reference, part.concat_total)
        groups.setdefault(key, []).append(part)

    for (_, _, total), grouped in groups.items():
        by_sequence = {
            part.concat_part: part
            for part in grouped
            if part.concat_part is not None
        }
        first = by_sequence.get(1)
        if first is None:
            continue

        normalized_sender = normalize_phone_sender(first.sender) or first.sender
        state_key = multipart_group_key(
            normalized_sender,
            first.concat_reference,
            first.concat_total,
            first.timestamp,
        )

        if set(by_sequence) == set(range(1, total + 1)):
            _multipart_first_seen.pop(state_key, None)
            _multipart_prealert_sent.discard(state_key)
            continue

        first_seen = _multipart_first_seen.setdefault(state_key, time.monotonic())
        waited = max(0.0, time.monotonic() - first_seen)
        if waited < SBR_PAGER_PREALERT_DELAY_SECONDS:
            continue
        if state_key in _multipart_prealert_sent:
            continue

        sender = normalize_phone_sender(first.sender)
        if sender is None:
            continue

        first_text = (first.text or "").strip()
        if not first_text or normalized_command(first_text) in SBR_PAGER_IGNORE_COMMANDS:
            continue

        is_sending_2 = _SENDING_2_PATTERN.search(first_text) is not None
        auto_parent_event_key = (
            None if is_sending_2 else active_followup_parent_key(sender, first_text)
        )
        is_auto_followup = auto_parent_event_key is not None
        station = detect_station_code(first_text)

        if is_sending_2:
            heading = "📟 *SENDING 2 MODTAGET*"
            message_kind = "sending2_prealert"
            parent_event_key = recent_parent_event_key(sender)
        elif is_auto_followup:
            heading = "📟 *OPFØLGNING MODTAGET*"
            message_kind = "followup_prealert"
            parent_event_key = auto_parent_event_key
        else:
            heading = "🚨 *ALARM MODTAGET*"
            message_kind = "alarm_prealert"
            parent_event_key = None

        visible_parts = len(by_sequence)
        body = (
            f"{heading}\n"
            f"{first_text}\n\n"
            f"⏳ Resten af meldingen er på vej · {visible_parts}/{total}"
        )

        try:
            result = post_sbr_payload(
                sender=sender,
                body=body,
                received_at=first.timestamp,
                source_message_id=prealert_source_id(first),
                event_key=state_key,
                group_key=state_key,
                message_kind=message_kind,
                raw_body=first_text,
                part_current=visible_parts,
                part_total=total,
                parent_event_key=parent_event_key,
            )
            if result is not None:
                _multipart_prealert_sent.add(state_key)
                if (
                    result.get("accepted")
                    and not is_sending_2
                    and not is_auto_followup
                    and station
                ):
                    remember_parent_event(sender, state_key, station)
            if result is not None and not result.get("duplicate", False):
                log.info(
                    "PRE-ALARM sendt efter %.1f s ventetid for multipart SMS fra %s, del=%s/%s, accepted=%s, sent=%s, failed=%s",
                    waited,
                    sender,
                    visible_parts,
                    total,
                    result.get("accepted"),
                    result.get("sent"),
                    result.get("failed"),
                )
        except Exception:  # noqa: BLE001
            log.exception("Kunne ikke sende multipart PRE-ALARM til SBR Pager")


def parse_cmgl_with_prealerts(response: str) -> list[dict]:
    parts = sms_pdu.parse_cmgl_parts(response)
    send_multipart_prealerts(parts)
    messages = sms_pdu.assemble_parts(parts)
    part_by_index = {part.index: part for part in parts}

    for message in messages:
        indices = message.get("indices") or []
        if len(indices) <= 1:
            continue
        first = part_by_index.get(indices[0])
        if first is None or first.concat_reference is None or first.concat_total is None:
            continue
        normalized_sender = normalize_phone_sender(first.sender) or first.sender
        message["_concat_reference"] = first.concat_reference
        message["_concat_total"] = first.concat_total
        message["_event_group_key"] = multipart_group_key(
            normalized_sender,
            first.concat_reference,
            first.concat_total,
            first.timestamp,
        )

    return messages


def post_message(message: dict):
    normalized_sender = normalize_phone_sender(message.get("sender") or "")
    if normalized_sender is None:
        log.warning(
            "Springer ikke-telefon SMS-afsender over og rydder beskeden fra modemmet: %r",
            message.get("sender"),
        )
        return {
            "station": None,
            "forwarded_immediately_to": 0,
            "vagtbytte_created": False,
        }

    if normalized_sender != message.get("sender"):
        message = dict(message)
        message["sender"] = normalized_sender

    command = normalized_command(message.get("body") or "")
    if command in SBR_PAGER_IGNORE_COMMANDS:
        log.info("SBR Pager ignorerer SMS-kommando fra %s: %s", message["sender"], command)
    else:
        result = post_to_sbr_pager(message)
        log.info(
            "SMS fra %s afleveret DIREKTE fra modem til SBR Pager, accepted=%s, sent=%s, failed=%s, duplicate=%s",
            message["sender"],
            (result or {}).get("accepted"),
            (result or {}).get("sent"),
            (result or {}).get("failed"),
            (result or {}).get("duplicate", False),
        )

    return _original_post_message(message)


reader.command = modem_command
reader.parse_cmgl_response = parse_cmgl_with_prealerts
reader.post_message = post_message


if __name__ == "__main__":
    log.info(
        "SBR Pager multipart pre-alert ventetid: %.1f sekunder; automatisk opfølgningsvindue: %.1f sekunder",
        SBR_PAGER_PREALERT_DELAY_SECONDS,
        SBR_PAGER_FOLLOWUP_WINDOW_SECONDS,
    )
    signal.signal(signal.SIGTERM, reader.stop)
    signal.signal(signal.SIGINT, reader.stop)
    reader.run()
