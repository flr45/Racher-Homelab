from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sms-whatsapp-poller")

SMS_GATEWAY_URL = os.getenv("SMS_GATEWAY_URL", "http://sms-gateway:8080").rstrip("/")
SMS_GATEWAY_API_TOKEN = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
INGEST_URL = os.getenv("SMS_WHATSAPP_LOCAL_INGEST_URL", "http://127.0.0.1:8080/api/incoming")
INGEST_TOKEN = os.getenv("SMS_WHATSAPP_INGEST_TOKEN", "").strip()
POLL_SECONDS = max(1.0, float(os.getenv("SMS_WHATSAPP_POLL_SECONDS", "2")))
STATE_FILE = Path(os.getenv("SMS_WHATSAPP_POLL_STATE_FILE", "/data/poll-state.json"))
IGNORE_COMMANDS = {
    " ".join(value.casefold().split())
    for value in os.getenv(
        "SMS_WHATSAPP_IGNORE_COMMANDS",
        "status,server status,serverstatus",
    ).split(",")
    if value.strip()
}


def request_json(url: str, token: str, method: str = "GET", payload: dict | None = None):
    data = None
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=12) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def read_last_seen() -> int | None:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(value.get("last_seen_id"))
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_last_seen(message_id: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_seen_id": int(message_id)}), encoding="utf-8")
    tmp.replace(STATE_FILE)


def normalized_command(body: str) -> str:
    return " ".join((body or "").strip().casefold().split())


def forward_message(message: dict) -> None:
    request_json(
        INGEST_URL,
        INGEST_TOKEN,
        method="POST",
        payload={
            "sender": message.get("sender"),
            "body": message.get("body"),
            "receivedAt": message.get("received_at"),
            "sourceMessageId": f"sms-gateway:{message['id']}",
        },
    )


def run() -> None:
    if not SMS_GATEWAY_API_TOKEN:
        raise RuntimeError("SMS_GATEWAY_API_TOKEN mangler")
    if not INGEST_TOKEN:
        raise RuntimeError("SMS_WHATSAPP_INGEST_TOKEN mangler")

    last_seen = read_last_seen()
    log.info("SMS→WhatsApp poller startet; last_seen=%s", last_seen)

    while True:
        try:
            messages = request_json(
                f"{SMS_GATEWAY_URL}/api/messages",
                SMS_GATEWAY_API_TOKEN,
            )
            if not isinstance(messages, list):
                raise RuntimeError("SMS-gateway returnerede ikke en liste")

            ordered = sorted(
                (item for item in messages if isinstance(item, dict) and item.get("id") is not None),
                key=lambda item: int(item["id"]),
            )

            if last_seen is None:
                last_seen = max((int(item["id"]) for item in ordered), default=0)
                write_last_seen(last_seen)
                log.info("Første start: starter efter SMS-id %s; historik videresendes ikke", last_seen)
                time.sleep(POLL_SECONDS)
                continue

            for message in ordered:
                message_id = int(message["id"])
                if message_id <= last_seen:
                    continue

                if normalized_command(str(message.get("body") or "")) in IGNORE_COMMANDS:
                    log.info("Ignorerer SMS-kommando id=%s", message_id)
                else:
                    forward_message(message)
                    log.info("SMS id=%s afleveret til WhatsApp-gateway", message_id)

                last_seen = message_id
                write_last_seen(last_seen)

        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, RuntimeError) as exc:
            log.warning("Poller kunne ikke behandle SMS: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Uventet fejl i SMS→WhatsApp poller: %s", exc)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
