import hashlib
import json
import logging
import os
import re
import signal
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import serial

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MODEM_DEVICE = os.getenv("MODEM_DEVICE", "/dev/ttyUSB1")
MODEM_BAUDRATE = int(os.getenv("MODEM_BAUDRATE", "115200"))
POLL_SECONDS = float(os.getenv("MODEM_POLL_SECONDS", "2"))
API_URL = os.getenv("INCOMING_API_URL", "http://127.0.0.1:8080/api/incoming")
STATUS_FILE = Path(os.getenv("MODEM_STATUS_FILE", "/data/modem-status.json"))
DELETE_AFTER_IMPORT = os.getenv("DELETE_SMS_AFTER_IMPORT", "true").lower() == "true"

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sms-modem-reader")
running = True

CMGL_HEADER = re.compile(
    r'^\+CMGL:\s*(?P<index>\d+),"(?P<status>[^"]*)","(?P<sender>[^"]*)"(?:,"[^"]*")?(?:,"(?P<timestamp>[^"]*)")?.*$'
)
MODEM_TIMESTAMP = re.compile(
    r"^(?P<year>\d{2})/(?P<month>\d{2})/(?P<day>\d{2}),"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<sign>[+-])(?P<quarters>\d{2})$"
)


def utc_iso():
    return datetime.now(timezone.utc).isoformat()


def api_utc_iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_modem_timestamp(value):
    match = MODEM_TIMESTAMP.fullmatch((value or "").strip())
    if not match:
        return api_utc_iso(datetime.now(timezone.utc))

    offset_minutes = int(match.group("quarters")) * 15
    if match.group("sign") == "-":
        offset_minutes *= -1
    zone = timezone(timedelta(minutes=offset_minutes))
    timestamp = datetime(
        year=2000 + int(match.group("year")),
        month=int(match.group("month")),
        day=int(match.group("day")),
        hour=int(match.group("hour")),
        minute=int(match.group("minute")),
        second=int(match.group("second")),
        tzinfo=zone,
    )
    return api_utc_iso(timestamp)


def source_message_id(message):
    stable_value = "|".join(
        [
            MODEM_DEVICE,
            str(message["index"]),
            message.get("timestamp") or "",
            message["sender"],
            message["body"],
        ]
    )
    digest = hashlib.sha256(stable_value.encode("utf-8")).hexdigest()
    return f"huawei-sms:{digest}"


def write_status(**values):
    current = {}
    try:
        current = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    current.update(values, updated_at=utc_iso(), device=MODEM_DEVICE)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATUS_FILE)


def stop(_signum, _frame):
    global running
    running = False


def command(port, value, timeout=8):
    port.reset_input_buffer()
    port.write((value + "\r").encode("ascii"))
    deadline = time.monotonic() + timeout
    response = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            response.extend(chunk)
            text = response.decode("utf-8", errors="replace")
            if "\r\nOK\r\n" in text or "\r\nERROR\r\n" in text or "+CME ERROR:" in text:
                return text
    raise TimeoutError(f"Modemmet svarede ikke på {value}")


def parse_messages(response):
    messages = []
    current = None
    body_lines = []

    for raw_line in response.replace("\r", "").split("\n"):
        line = raw_line.strip("\x00")
        match = CMGL_HEADER.match(line)
        if match:
            if current:
                current["body"] = "\n".join(body_lines).strip()
                messages.append(current)
            current = match.groupdict()
            current["index"] = int(current["index"])
            body_lines = []
            continue
        if current and line not in {"OK", "ERROR"}:
            body_lines.append(line)

    if current:
        current["body"] = "\n".join(body_lines).strip()
        messages.append(current)
    return [message for message in messages if message["sender"] and message["body"]]


def post_message(message):
    payload = json.dumps(
        {
            "sender": message["sender"],
            "body": message["body"],
            "receivedAt": parse_modem_timestamp(message.get("timestamp")),
            "sourceMessageId": source_message_id(message),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status not in {200, 201}:
            raise RuntimeError(f"Gateway API svarede HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def initialize(port):
    for value in ("AT", "ATE0", "AT+CMGF=1", 'AT+CPMS="SM","SM","SM"'):
        response = command(port, value)
        if "ERROR" in response:
            raise RuntimeError(f"Modemmet afviste {value}: {response.strip()}")
    network = command(port, "AT+CREG?")
    signal_quality = command(port, "AT+CSQ")
    write_status(state="online", network=network.strip(), signal=signal_quality.strip(), last_error=None)


def run():
    retry_seconds = 2
    while running:
        try:
            write_status(state="connecting", last_error=None)
            with serial.Serial(MODEM_DEVICE, MODEM_BAUDRATE, timeout=0.3) as port:
                initialize(port)
                retry_seconds = 2
                log.info("Huawei-modem online på %s", MODEM_DEVICE)

                while running:
                    response = command(port, 'AT+CMGL="REC UNREAD"', timeout=10)
                    messages = parse_messages(response)
                    for message in messages:
                        try:
                            result = post_message(message)
                            log.info(
                                "SMS %s fra %s importeret, station=%s, modtagere=%s, vagtbytte=%s",
                                message["index"],
                                message["sender"],
                                result.get("station"),
                                result.get("forwarded_immediately_to"),
                                "oprettet" if result.get("vagtbytte_created") else "dublet",
                            )
                            if DELETE_AFTER_IMPORT:
                                delete_response = command(port, f'AT+CMGD={message["index"]}')
                                if "ERROR" in delete_response:
                                    raise RuntimeError(f"Kunne ikke slette SMS {message['index']}")
                            write_status(
                                state="online",
                                last_message_at=utc_iso(),
                                last_sender=message["sender"],
                                last_error=None,
                            )
                        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
                            log.exception("Kunne ikke behandle SMS %s", message["index"])
                            write_status(state="degraded", last_error=str(exc))
                    time.sleep(POLL_SECONDS)
        except (serial.SerialException, OSError, TimeoutError, RuntimeError) as exc:
            log.exception("Modemforbindelsen fejlede")
            write_status(state="offline", last_error=str(exc))
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 60)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run()
