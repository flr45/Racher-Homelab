import hashlib
import json
import logging
import os
import re
import signal
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import serial

from gsm0338 import encode_gsm0338
from sms_pdu import parse_cmgl_response

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MODEM_DEVICE = os.getenv("MODEM_DEVICE", "/dev/ttyUSB1")
MODEM_BAUDRATE = int(os.getenv("MODEM_BAUDRATE", "115200"))
MODEM_DISABLE_DTR_TOGGLE = (
    os.getenv("MODEM_DISABLE_DTR_TOGGLE", "true").lower() == "true"
)
POLL_SECONDS = float(os.getenv("MODEM_POLL_SECONDS", "2"))
API_BASE_URL = os.getenv("GATEWAY_API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
INCOMING_API_URL = os.getenv("INCOMING_API_URL", f"{API_BASE_URL}/api/incoming")
OUTGOING_CLAIM_URL = os.getenv(
    "OUTGOING_CLAIM_URL", f"{API_BASE_URL}/api/outgoing/claim"
)
STATUS_FILE = Path(os.getenv("MODEM_STATUS_FILE", "/data/modem-status.json"))
DELETE_AFTER_IMPORT = (
    os.getenv("DELETE_SMS_AFTER_IMPORT", "true").lower() == "true"
)
SMS_DRY_RUN = os.getenv("SMS_DRY_RUN", "false").lower() == "true"
SMS_SEND_TIMEOUT_SECONDS = max(
    25, int(os.getenv("SMS_SEND_TIMEOUT_SECONDS", "60"))
)
OUTBOX_BATCH_SIZE = max(1, int(os.getenv("SMS_OUTBOX_BATCH_SIZE", "20")))
MODEM_NETWORK_CHECK_SECONDS = max(
    10.0,
    float(os.getenv("MODEM_NETWORK_CHECK_SECONDS", "30")),
)
MODEM_NETWORK_FAILURES_BEFORE_RESET = max(
    1,
    int(os.getenv("MODEM_NETWORK_FAILURES_BEFORE_RESET", "4")),
)
MODEM_NETWORK_RADIO_RESET_ENABLED = (
    os.getenv("MODEM_NETWORK_RADIO_RESET_ENABLED", "true").lower() == "true"
)
MODEM_NETWORK_RECOVERY_SETTLE_SECONDS = max(
    2.0,
    float(os.getenv("MODEM_NETWORK_RECOVERY_SETTLE_SECONDS", "8")),
)
_NETWORK_REGISTRATION_RE = re.compile(r"\+CREG:\s*\d+\s*,\s*(\d+)")
_CFUN_RE = re.compile(r"\+CFUN:\s*(\d+)")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sms-modem-reader")
running = True


def utc_iso():
    return datetime.now(timezone.utc).isoformat()


def source_message_id(message):
    stable_value = "|".join(
        [
            MODEM_DEVICE,
            ",".join(str(index) for index in message["indices"]),
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


def network_registration_status(response: str) -> int | None:
    match = _NETWORK_REGISTRATION_RE.search(response or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def network_is_registered(response: str) -> bool:
    return network_registration_status(response) in {1, 5}


def cfun_mode(response: str) -> int | None:
    match = _CFUN_RE.search(response or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def wait_for_sim_ready(port, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + max(1.0, timeout)
    last_response = ""
    while time.monotonic() < deadline:
        last_response = command(port, "AT+CPIN?", timeout=8)
        if "+CPIN: READY" in last_response:
            return
        time.sleep(2)
    raise RuntimeError(
        "SIM blev ikke READY efter radioaktivering: "
        + (last_response.strip() or "intet svar")
    )


def ensure_radio_enabled(port) -> None:
    response = command(port, "AT+CFUN?", timeout=8)
    mode = cfun_mode(response)
    if mode == 1:
        return

    log.warning(
        "SIM800 radio er ikke i fuld funktion (CFUN=%s); aktiverer med AT+CFUN=1",
        "ukendt" if mode is None else mode,
    )
    write_status(
        state="recovering",
        recovery_reason=f"CFUN={mode}",
        last_recovery_at=utc_iso(),
        last_error="SIM800 radio var deaktiveret",
    )
    response = command(port, "AT+CFUN=1", timeout=20)
    if "ERROR" in response:
        raise RuntimeError(f"Modemmet afviste AT+CFUN=1: {response.strip()}")
    wait_for_sim_ready(port)
    time.sleep(MODEM_NETWORK_RECOVERY_SETTLE_SECONDS)


def recover_radio(port) -> None:
    if not MODEM_NETWORK_RADIO_RESET_ENABLED:
        log.warning("Automatisk radio-reset er deaktiveret")
        return

    log.warning("Forsøger automatisk SIM800 radio-reset med AT+CFUN")
    try:
        current = command(port, "AT+CFUN?", timeout=8)
    except TimeoutError:
        current = ""
    mode = cfun_mode(current)

    if mode == 1:
        try:
            off = command(port, "AT+CFUN=0", timeout=12)
            if "ERROR" in off and "CFUN state is 0" not in off:
                raise RuntimeError(f"Modemmet afviste AT+CFUN=0: {off.strip()}")
        except TimeoutError:
            # SIM800C kan skifte til CFUN=0 uden at sende et stabilt afsluttende
            # OK-svar. Fortsæt derfor med at tænde radioen igen.
            log.warning("AT+CFUN=0 gav timeout; fortsætter med radioaktivering")
        time.sleep(2)

    ensure_radio_enabled(port)


def command(port, value, timeout=8, expected="\r\nOK\r\n"):
    port.reset_input_buffer()
    port.write((value + "\r").encode("ascii"))
    port.flush()
    deadline = time.monotonic() + timeout
    response = bytearray()

    while time.monotonic() < deadline:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            response.extend(chunk)
            text = response.decode("ascii", errors="replace")
            if expected in text:
                return text
            if any(
                marker in text
                for marker in ("\r\nERROR\r\n", "+CME ERROR:", "+CMS ERROR:")
            ):
                return text

    raise TimeoutError(f"Modemmet svarede ikke på {value}")


def api_json(url, method="GET", payload=None, timeout=15):
    data = None
    headers = {}
    token = os.getenv("SMS_GATEWAY_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    outgoing = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(outgoing, timeout=timeout) as response:
        if response.status == 204:
            return None
        body = response.read().decode("utf-8")
        if response.status not in {200, 201, 202}:
            raise RuntimeError(f"Gateway API svarede HTTP {response.status}")
        return json.loads(body) if body else None


def post_message(message):
    return api_json(
        INCOMING_API_URL,
        method="POST",
        payload={
            "sender": message["sender"],
            "body": message["body"],
            "receivedAt": message["timestamp"],
            "sourceMessageId": source_message_id(message),
        },
        timeout=15,
    )


def claim_outgoing():
    return api_json(
        OUTGOING_CLAIM_URL,
        method="POST",
        payload={},
        timeout=10,
    )


def complete_outgoing(message_id, status, error=None, retry=False):
    return api_json(
        f"{API_BASE_URL}/api/outgoing/{message_id}/complete",
        method="POST",
        payload={
            "status": status,
            "error": error,
            "retry": retry,
        },
        timeout=10,
    )


def initialize(port):
    for value in ("AT", "ATE0"):
        response = command(port, value)
        if "ERROR" in response:
            raise RuntimeError(f"Modemmet afviste {value}: {response.strip()}")

    # SMS-kommandoer som CMGF/CPMS fejler på SIM800C, når radioen står i
    # CFUN=0. Aktiver derfor radio/SIM først, så en gateway-genstart også kan
    # selv-heale efter en deaktiveret radio.
    ensure_radio_enabled(port)

    for value in (
        "AT+CMGF=0",
        'AT+CPMS="SM","SM","SM"',
    ):
        response = command(port, value)
        if "ERROR" in response:
            raise RuntimeError(f"Modemmet afviste {value}: {response.strip()}")
    network = command(port, "AT+CREG?")
    signal_quality = command(port, "AT+CSQ")
    registered = network_is_registered(network)
    write_status(
        state="online" if registered else "degraded",
        network=network.strip(),
        signal=signal_quality.strip(),
        network_failures=0 if registered else 1,
        last_error=None if registered else "Mobilnet ikke registreret endnu",
    )


def send_outgoing_sms(port, recipient, body):
    if SMS_DRY_RUN:
        log.info("DRY RUN SMS til %s: %s", recipient, body)
        return

    try:
        for value in (
            "AT",
            'AT+CSCS="GSM"',
            "AT+CMGF=1",
        ):
            response = command(port, value)
            if "ERROR" in response:
                raise RuntimeError(f"Modemmet afviste {value}: {response.strip()}")

        prompt = command(
            port,
            f'AT+CMGS="{recipient}"',
            timeout=15,
            expected=">",
        )
        if ">" not in prompt:
            raise RuntimeError(
                f"Modemmet afviste SMS-modtageren: {prompt.strip()}"
            )

        port.write(encode_gsm0338(body) + b"\x1a")
        port.flush()
        deadline = time.monotonic() + SMS_SEND_TIMEOUT_SECONDS
        response = bytearray()

        while time.monotonic() < deadline:
            chunk = port.read(port.in_waiting or 1)
            if chunk:
                response.extend(chunk)
                text = response.decode("ascii", errors="replace")
                if "+CMGS:" in text and "\r\nOK\r\n" in text:
                    return
                if any(
                    marker in text
                    for marker in (
                        "\r\nERROR\r\n",
                        "+CME ERROR:",
                        "+CMS ERROR:",
                    )
                ):
                    raise RuntimeError(text.strip())

        raise TimeoutError("SMS-afsendelse fik ikke kvittering fra modem")
    finally:
        try:
            command(port, "AT+CMGF=0", timeout=8)
        except Exception:  # noqa: BLE001
            log.exception("Kunne ikke gendanne modemmet til PDU-tilstand")


def process_outbox(port):
    processed = 0
    while running and processed < OUTBOX_BATCH_SIZE:
        message = claim_outgoing()
        if message is None:
            return

        message_id = message["id"]
        recipient = message["recipient"]
        try:
            send_outgoing_sms(port, recipient, message["body"])
            complete_outgoing(message_id, "sent")
            write_status(
                state="online",
                last_sent_sms_at=utc_iso(),
                last_sent_recipient=recipient,
                last_error=None,
            )
            log.info("Udgående SMS %s sendt til %s", message_id, recipient)
        except (serial.SerialException, OSError, TimeoutError) as exc:
            result = complete_outgoing(
                message_id,
                "failed",
                error=str(exc),
                retry=True,
            )
            log.exception(
                "Udgående SMS %s fejlede%s",
                message_id,
                " og er sat i kø igen" if result.get("retried") else "",
            )
            write_status(state="degraded", last_error=str(exc))
            raise
        except (RuntimeError, ValueError) as exc:
            result = complete_outgoing(
                message_id,
                "failed",
                error=str(exc),
                retry=True,
            )
            log.exception(
                "Udgående SMS %s fejlede%s",
                message_id,
                " og er sat i kø igen" if result.get("retried") else "",
            )
            write_status(state="degraded", last_error=str(exc))
            if result.get("retried"):
                return

        processed += 1


def run():
    retry_seconds = 2
    while running:
        try:
            write_status(state="connecting", last_error=None)
            with serial.Serial(
                MODEM_DEVICE,
                MODEM_BAUDRATE,
                timeout=0.3,
                dsrdtr=MODEM_DISABLE_DTR_TOGGLE,
            ) as port:
                initialize(port)
                retry_seconds = 2
                log.info(
                    "SMS-modem online på %s; én proces ejer serieporten",
                    MODEM_DEVICE,
                )

                network_failures = 0
                next_network_check = 0.0

                while running:
                    now = time.monotonic()
                    if now >= next_network_check:
                        network = command(port, "AT+CREG?")
                        signal_quality = command(port, "AT+CSQ")
                        if network_is_registered(network):
                            if network_failures:
                                log.info(
                                    "SMS-modem er registreret på mobilnettet igen efter %s fejlede kontroller",
                                    network_failures,
                                )
                            network_failures = 0
                            write_status(
                                state="online",
                                network=network.strip(),
                                signal=signal_quality.strip(),
                                network_failures=0,
                                last_error=None,
                            )
                        else:
                            network_failures += 1
                            registration = network_registration_status(network)
                            error = (
                                "Mobilnet ikke registreret"
                                if registration is None
                                else f"Mobilnet ikke registreret (CREG={registration})"
                            )
                            write_status(
                                state="degraded",
                                network=network.strip(),
                                signal=signal_quality.strip(),
                                network_failures=network_failures,
                                last_error=error,
                            )
                            log.warning(
                                "%s; kontrol %s/%s",
                                error,
                                network_failures,
                                MODEM_NETWORK_FAILURES_BEFORE_RESET,
                            )
                            if network_failures >= MODEM_NETWORK_FAILURES_BEFORE_RESET:
                                write_status(
                                    state="recovering",
                                    last_recovery_at=utc_iso(),
                                    recovery_reason=error,
                                    last_error=error,
                                )
                                recover_radio(port)
                                raise RuntimeError(
                                    f"{error}; radio-reset udført og serieport genåbnes"
                                )
                        next_network_check = now + MODEM_NETWORK_CHECK_SECONDS

                    response = command(port, "AT+CMGL=0", timeout=15)
                    messages = parse_cmgl_response(response)

                    for message in messages:
                        message_label = "+".join(
                            str(index) for index in message["indices"]
                        )
                        try:
                            result = post_message(message)
                            log.info(
                                "SMS %s fra %s importeret, station=%s, modtagere=%s, vagtbytte=%s",
                                message_label,
                                message["sender"],
                                result.get("station"),
                                result.get("forwarded_immediately_to"),
                                "deaktiveret"
                                if result.get("vagtbytte_disabled")
                                else (
                                    "oprettet"
                                    if result.get("vagtbytte_created")
                                    else "dublet"
                                ),
                            )
                            if DELETE_AFTER_IMPORT:
                                for index in message["indices"]:
                                    delete_response = command(port, f"AT+CMGD={index}")
                                    if "ERROR" in delete_response:
                                        raise RuntimeError(
                                            f"Kunne ikke slette SMS {index}"
                                        )
                            write_status(
                                state="online",
                                last_message_at=utc_iso(),
                                last_sender=message["sender"],
                                last_error=None,
                            )
                        except (
                            urllib.error.URLError,
                            TimeoutError,
                            RuntimeError,
                            ValueError,
                        ) as exc:
                            log.exception("Kunne ikke behandle SMS %s", message_label)
                            write_status(state="degraded", last_error=str(exc))

                    process_outbox(port)
                    time.sleep(POLL_SECONDS)
        except (
            serial.SerialException,
            OSError,
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
            ValueError,
        ) as exc:
            log.exception("Modemforbindelsen fejlede")
            write_status(state="offline", last_error=str(exc))
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 60)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run()
