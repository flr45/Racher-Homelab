"""Direct SBR Pager delivery for inbound SMS.

This runtime layer forwards ordinary inbound SMS to SBR Pager before the
existing SMS gateway/Vagtbytte processing. SMS status commands keep their
existing local command path and are not sent to SBR Pager.
"""

import json
import logging
import os
import urllib.error
import urllib.request

import app as gateway

app = gateway.app
base = gateway.base
log = logging.getLogger("sms-gateway-sbr")

_original_process_incoming = gateway.process_incoming


def forward_to_sbr_pager(
    sender: str,
    body: str,
    received_at=None,
    source_message_id: str | None = None,
):
    url = os.getenv(
        "SBR_PAGER_INGEST_URL",
        "http://sms-whatsapp:8080/api/incoming",
    ).strip()
    token = os.getenv("SBR_PAGER_INGEST_TOKEN", "").strip()

    if not url:
        return None
    if not token:
        raise RuntimeError("SBR_PAGER_INGEST_TOKEN mangler")

    normalized_sender = base.normalize_phone(sender)
    normalized_body = (body or "").strip()
    if not normalized_body:
        raise ValueError("SMS-teksten er tom")

    parsed_received_at = base.parse_received_at(received_at)
    payload = {
        "sender": normalized_sender,
        "body": normalized_body,
        "receivedAt": parsed_received_at.isoformat(),
        "sourceMessageId": source_message_id,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            response_body = response.read().decode("utf-8")
            if response.status not in {200, 201, 202}:
                raise RuntimeError(f"SBR Pager svarede HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"SBR Pager svarede HTTP {exc.code}: {details[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Kunne ikke kontakte SBR Pager: {exc.reason}") from exc

    try:
        return json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("SBR Pager returnerede ugyldigt JSON") from exc


def process_incoming(*args, **kwargs):
    sender = kwargs.get("sender", args[0] if len(args) >= 1 else "")
    body = kwargs.get("body", args[1] if len(args) >= 2 else "")
    received_at = kwargs.get("received_at", args[2] if len(args) >= 3 else None)
    source_message_id = kwargs.get(
        "source_message_id",
        args[3] if len(args) >= 4 else None,
    )

    normalized_sender = base.normalize_phone(sender)
    detected_command = gateway.command_name(body)
    if detected_command and normalized_sender in gateway.allowed_command_senders():
        return _original_process_incoming(*args, **kwargs)

    result = forward_to_sbr_pager(
        sender=normalized_sender,
        body=body,
        received_at=received_at,
        source_message_id=source_message_id,
    )
    if result is not None:
        log.info(
            "SMS fra %s afleveret direkte til SBR Pager, accepted=%s, sent=%s, failed=%s, duplicate=%s",
            normalized_sender,
            result.get("accepted"),
            result.get("sent"),
            result.get("failed"),
            result.get("duplicate", False),
        )

    # Existing gateway/Vagtbytte handling runs only after SBR Pager has had the
    # message. If it later fails, modem_reader retains the SMS and retries; SBR
    # Pager deduplicates retries by sourceMessageId.
    return _original_process_incoming(*args, **kwargs)


# The Flask view registered in base_app resolves this global at request time.
gateway.process_incoming = process_incoming
base.process_incoming = process_incoming
