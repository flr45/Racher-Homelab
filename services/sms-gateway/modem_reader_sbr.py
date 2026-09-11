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

For multipart alarm messages, part 1 is also sent immediately as a deduplicated
pre-alert. When every segment has arrived, the normal complete message follows.
A later multipart "Sending 2" gets its own immediate follow-up pre-alert.

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
import urllib.error
import urllib.request

import modem_reader as reader
import sms_pdu

log = logging.getLogger("sms-modem-reader-sbr")

SBR_PAGER_INGEST_URL = os.getenv(
    "SBR_PAGER_INGEST_URL",
    "http://sms-whatsapp:8080/api/incoming",
).strip()
SBR_PAGER_INGEST_TOKEN = os.getenv("SBR_PAGER_INGEST_TOKEN", "").strip()
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
_SENDING_2_PATTERN = re.compile(r"\bsending\s*2\b", re.IGNORECASE)


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
    # List ALL stored SMS instead of only unread SMS. Some Huawei firmware marks
    # a returned unread segment as read, which otherwise loses multipart groups
    # when their segments arrive in different polling cycles.
    if value == "AT+CMGL=0":
        value = "AT+CMGL=4"
    return _original_command(port, value, timeout=timeout, expected=expected)


def post_sbr_payload(
    sender: str,
    body: str,
    received_at: str,
    source_message_id: str,
):
    if not SBR_PAGER_INGEST_URL:
        return None
    if not SBR_PAGER_INGEST_TOKEN:
        raise RuntimeError("SBR_PAGER_INGEST_TOKEN mangler")

    payload = json.dumps(
        {
            "sender": sender,
            "body": body,
            "receivedAt": received_at,
            "sourceMessageId": source_message_id,
        },
        ensure_ascii=False,
    ).encode("utf-8")
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


def post_to_sbr_pager(message: dict):
    return post_sbr_payload(
        sender=message["sender"],
        body=message["body"],
        received_at=message["timestamp"],
        source_message_id=reader.source_message_id(message),
    )


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

        # The complete message is handled by the normal path below. Pre-alerts
        # are only for the period where later fragments are still missing.
        if set(by_sequence) == set(range(1, total + 1)):
            continue

        first = by_sequence.get(1)
        if first is None:
            continue

        sender = normalize_phone_sender(first.sender)
        if sender is None:
            continue

        first_text = (first.text or "").strip()
        if not first_text or normalized_command(first_text) in SBR_PAGER_IGNORE_COMMANDS:
            continue

        is_sending_2 = _SENDING_2_PATTERN.search(first_text) is not None
        heading = "📟 SENDING 2 MODTAGET" if is_sending_2 else "🚨 ALARM MODTAGET"
        visible_parts = len(by_sequence)
        body = (
            f"{heading}\n"
            f"{first_text}\n\n"
            f"⏳ Afventer resten af meldingen ({visible_parts}/{total})"
        )

        try:
            result = post_sbr_payload(
                sender=sender,
                body=body,
                received_at=first.timestamp,
                source_message_id=prealert_source_id(first),
            )
            if result is not None and not result.get("duplicate", False):
                log.info(
                    "PRE-ALARM sendt straks for multipart SMS fra %s, del=%s/%s, accepted=%s, sent=%s, failed=%s",
                    sender,
                    visible_parts,
                    total,
                    result.get("accepted"),
                    result.get("sent"),
                    result.get("failed"),
                )
        except Exception:  # noqa: BLE001
            # A failed pre-alert must never stop the modem reader. It will be
            # retried on the next 0.5 s poll while the fragments remain stored.
            log.exception("Kunne ikke sende multipart PRE-ALARM til SBR Pager")


def parse_cmgl_with_prealerts(response: str) -> list[dict]:
    parts = sms_pdu.parse_cmgl_parts(response)
    send_multipart_prealerts(parts)
    return sms_pdu.assemble_parts(parts)


def post_message(message: dict):
    normalized_sender = normalize_phone_sender(message.get("sender") or "")
    if normalized_sender is None:
        log.warning(
            "Springer ikke-telefon SMS-afsender over og rydder beskeden fra modemmet: %r",
            message.get("sender"),
        )
        # Return a normal-looking gateway result so modem_reader can delete the
        # unsupported service/operator SMS instead of retrying it forever.
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

    # Keep the existing SMS-gateway/Vagtbytte flow as an independent secondary
    # consumer. The modem message is only deleted after this returns normally.
    return _original_post_message(message)


reader.command = modem_command
reader.parse_cmgl_response = parse_cmgl_with_prealerts
reader.post_message = post_message


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, reader.stop)
    signal.signal(signal.SIGINT, reader.stop)
    reader.run()
