"""Decode SMS-DELIVER PDUs returned by Huawei modems."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from gsm0338 import decode_septets, unpack_septets

_CMGL_HEADER = re.compile(r"^\+CMGL:\s*(?P<index>\d+),(?P<status>\d+),.*$")
_HEX_LINE = re.compile(r"^[0-9A-Fa-f]+$")


@dataclass(frozen=True)
class DecodedSmsPart:
    index: int
    sender: str
    text: str
    timestamp: str
    concat_reference: int | None = None
    concat_total: int | None = None
    concat_part: int | None = None


def parse_cmgl_response(response: str) -> list[dict]:
    """Parse and assemble unread SMS messages from an AT+CMGL PDU response."""

    lines = [line.strip() for line in response.replace("\r", "").split("\n")]
    parts: list[DecodedSmsPart] = []

    index = 0
    while index < len(lines):
        match = _CMGL_HEADER.match(lines[index])
        if not match:
            index += 1
            continue

        pdu_line = ""
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            if _CMGL_HEADER.match(candidate):
                break
            if candidate and _HEX_LINE.fullmatch(candidate) and len(candidate) % 2 == 0:
                pdu_line = candidate
                break
            cursor += 1

        if pdu_line:
            parts.append(decode_sms_deliver_pdu(int(match.group("index")), pdu_line))
        index = max(cursor + 1, index + 1)

    return assemble_parts(parts)


def decode_sms_deliver_pdu(index: int, pdu_hex: str) -> DecodedSmsPart:
    """Decode one SMS-DELIVER PDU."""

    data = bytes.fromhex(pdu_hex)
    position = 0

    if not data:
        raise ValueError("Tom SMS-PDU")

    smsc_length = data[position]
    position += 1 + smsc_length
    if position >= len(data):
        raise ValueError("Ugyldig SMSC-længde i PDU")

    first_octet = data[position]
    position += 1
    if first_octet & 0x03 != 0:
        raise ValueError("PDU'en er ikke en SMS-DELIVER")

    sender_length = data[position]
    position += 1
    sender_type = data[position]
    position += 1
    sender_octets = (sender_length + 1) // 2

    if position + sender_octets > len(data):
        raise ValueError("Afsendernummer mangler i PDU")

    sender_data = data[position : position + sender_octets]
    position += sender_octets
    sender = _decode_address(sender_data, sender_length, sender_type)

    if position + 10 > len(data):
        raise ValueError("PDU-headeren er afkortet")

    position += 1  # TP-PID
    dcs = data[position]
    position += 1
    timestamp = _decode_timestamp(data[position : position + 7])
    position += 7
    user_data_length = data[position]
    position += 1

    alphabet = _alphabet_from_dcs(dcs)
    if alphabet == "gsm7":
        user_data_octets = math.ceil(user_data_length * 7 / 8)
    else:
        user_data_octets = user_data_length

    user_data = data[position : position + user_data_octets]
    if len(user_data) < user_data_octets:
        raise ValueError("SMS-brugerdata er afkortet")

    header_length = 0
    concat_reference = None
    concat_total = None
    concat_part = None

    if first_octet & 0x40:
        if not user_data:
            raise ValueError("PDU markerer UDH, men brugerdata mangler")
        header_length = 1 + user_data[0]
        if header_length > len(user_data):
            raise ValueError("UDH-længden er ugyldig")
        concat_reference, concat_total, concat_part = _parse_concat_header(
            user_data[1:header_length]
        )

    if alphabet == "gsm7":
        header_septets = math.ceil(header_length * 8 / 7) if header_length else 0
        text_septets = max(0, user_data_length - header_septets)
        values = unpack_septets(
            user_data,
            text_septets,
            bit_offset=header_septets * 7,
        )
        text = decode_septets(values)
    elif alphabet == "ucs2":
        text = user_data[header_length:].decode("utf-16-be", errors="replace")
    else:
        text = user_data[header_length:].decode("latin-1", errors="replace")

    return DecodedSmsPart(
        index=index,
        sender=sender,
        text=text,
        timestamp=timestamp,
        concat_reference=concat_reference,
        concat_total=concat_total,
        concat_part=concat_part,
    )


def assemble_parts(parts: list[DecodedSmsPart]) -> list[dict]:
    """Join complete concatenated SMS messages and leave incomplete groups unread."""

    messages: list[dict] = []
    multipart: dict[tuple[str, int, int], list[DecodedSmsPart]] = {}

    for part in parts:
        if (
            part.concat_reference is None
            or part.concat_total is None
            or part.concat_part is None
        ):
            messages.append(_message_from_parts([part]))
            continue

        key = (part.sender, part.concat_reference, part.concat_total)
        multipart.setdefault(key, []).append(part)

    for (_, _, total), grouped_parts in multipart.items():
        by_sequence = {
            part.concat_part: part
            for part in grouped_parts
            if part.concat_part is not None
        }
        if set(by_sequence) != set(range(1, total + 1)):
            continue

        ordered = [by_sequence[sequence] for sequence in range(1, total + 1)]
        messages.append(_message_from_parts(ordered))

    return sorted(messages, key=lambda message: message["indices"][0])


def _message_from_parts(parts: list[DecodedSmsPart]) -> dict:
    return {
        "index": parts[0].index,
        "indices": [part.index for part in parts],
        "sender": parts[0].sender,
        "body": "".join(part.text for part in parts).strip(),
        "timestamp": parts[0].timestamp,
    }


def _decode_address(data: bytes, digits: int, address_type: int) -> str:
    if address_type & 0x70 == 0x50:
        septet_count = digits
        text = decode_septets(unpack_septets(data, septet_count))
        return text

    value = "".join(
        f"{byte & 0x0F:X}{(byte >> 4) & 0x0F:X}"
        for byte in data
    )[:digits].replace("F", "")

    if address_type & 0x90 == 0x90:
        return f"+{value}"
    return value


def _decode_timestamp(data: bytes) -> str:
    if len(data) != 7:
        raise ValueError("SMS-tidspunktet mangler")

    values = [_swapped_decimal(byte) for byte in data[:6]]
    year = 2000 + values[0]

    timezone_byte = data[6]
    low_nibble = timezone_byte & 0x0F
    high_nibble = (timezone_byte >> 4) & 0x0F
    negative = bool(low_nibble & 0x08)
    quarter_hours = (low_nibble & 0x07) * 10 + high_nibble
    offset_minutes = quarter_hours * 15 * (-1 if negative else 1)

    local_time = datetime(
        year,
        values[1],
        values[2],
        values[3],
        values[4],
        values[5],
        tzinfo=timezone(timedelta(minutes=offset_minutes)),
    )
    return local_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _swapped_decimal(value: int) -> int:
    return (value & 0x0F) * 10 + ((value >> 4) & 0x0F)


def _alphabet_from_dcs(dcs: int) -> str:
    if dcs & 0xC0 == 0x00:
        alphabet = (dcs >> 2) & 0x03
        if alphabet == 0:
            return "gsm7"
        if alphabet == 2:
            return "ucs2"
        return "8bit"

    if dcs & 0xF0 == 0xF0:
        return "8bit" if dcs & 0x04 else "gsm7"

    if dcs & 0xC0 == 0xC0:
        return "ucs2" if dcs & 0x04 else "gsm7"

    return "gsm7"


def _parse_concat_header(header: bytes) -> tuple[int | None, int | None, int | None]:
    position = 0
    while position + 2 <= len(header):
        identifier = header[position]
        length = header[position + 1]
        value = header[position + 2 : position + 2 + length]
        position += 2 + length

        if len(value) != length:
            break
        if identifier == 0x00 and length == 3:
            return value[0], value[1], value[2]
        if identifier == 0x08 and length == 4:
            reference = (value[0] << 8) | value[1]
            return reference, value[2], value[3]

    return None, None, None
