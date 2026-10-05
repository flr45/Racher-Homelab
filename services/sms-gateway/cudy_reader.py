"""Read SMS through the LT300's stock LuCI AT form; reuse the USB alarm flow."""
import hashlib
import json
import logging
import os
import re
import signal
import time
import textwrap

import modem_reader_sbr as pager
from cudy_client import CudyClient, CudyError

reader = pager.reader
log = logging.getLogger("sms-cudy-reader")
POLL_SECONDS = max(2.0, float(os.getenv("CUDY_POLL_SECONDS", "5")))
SEND_ENABLED = os.getenv("CUDY_SMS_SEND_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
SMS_ENGINE_CHECK_SECONDS = max(
    10.0,
    float(os.getenv("CUDY_SMS_ENGINE_CHECK_SECONDS", "60")),
)
CAPABILITY = "send-receive" if SEND_ENABLED else "receive-only"


def registered(response):
    match = re.search(r"\+(?:CEREG|CGREG|CREG):\s*\d+\s*,\s*(\d+)", response or "")
    return bool(match and int(match[1]) in {1, 5})


def read_storage(client):
    try:
        response = client.command('AT+CPMS?')
        return response.strip() if '+CPMS:' in response else None
    except Exception:
        return None


def read_inbox(client):
    mode = client.command("AT+CMGF?")
    match = re.search(r"\+CMGF:\s*([01])", mode)
    if not match:
        raise CudyError("Routerens SMS-tilstand kunne ikke læses")
    previous_mode = match[1]
    try:
        if "ERROR" in client.command("AT+CMGF=0"):
            raise CudyError("Routeren understøtter ikke PDU-læsning")
        result = client.command("AT+CMGL=4")
        if "ERROR" in result:
            raise CudyError("Routerens SMS-lager kunne ikke læses")
        return pager.parse_cmgl_with_prealerts(result)
    finally:
        if "ERROR" in client.command(f"AT+CMGF={previous_mode}"):
            raise CudyError("Routerens oprindelige SMS-tilstand kunne ikke gendannes")


def import_message(client, message):
    # Keep the complete source message until both persistent applications have
    # acknowledged it. Stable IDs make a repeated read safe after a restart.
    result = pager.post_message(message)
    if reader.DELETE_AFTER_IMPORT:
        for index in message["indices"]:
            if "ERROR" in client.command(f"AT+CMGD={index}"):
                raise CudyError(f"SMS {index} er importeret men kunne ikke fjernes fra routeren")
    reader.write_status(state="online", last_message_at=reader.utc_iso(), last_sender=message["sender"], last_error=None)
    return result




def outgoing_sms_parts(message: dict) -> list[str]:
    """Create <=160-char parts with a stable per-queue-job marker."""
    body = " ".join(str(message.get("body") or "").split())
    if not body:
        raise ValueError("SMS-teksten er tom")

    stable = "|".join(
        [
            str(message.get("id") or ""),
            str(message.get("created_at") or ""),
            str(message.get("recipient") or ""),
        ]
    )
    tag = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:6].upper()
    single_prefix = f"[SBR {tag}] "
    if len(single_prefix) + len(body) <= 160:
        return [single_prefix + body]

    total_guess = 2
    while True:
        longest_prefix = f"[SBR {tag} {total_guess}/{total_guess}] "
        width = max(1, 160 - len(longest_prefix))
        chunks = textwrap.wrap(
            body,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=True,
            drop_whitespace=True,
        )
        total = len(chunks)
        if total == total_guess:
            break
        total_guess = total

    return [
        f"[SBR {tag} {index}/{total}] {chunk}"
        for index, chunk in enumerate(chunks, 1)
    ]


def process_outbox(client):
    """Deliver queued outgoing jobs through Cudy when explicitly enabled."""
    if not SEND_ENABLED:
        return

    processed = 0
    while reader.running and processed < reader.OUTBOX_BATCH_SIZE:
        message = reader.claim_outgoing()
        if message is None:
            return

        message_id = message["id"]
        recipient = message["recipient"]
        parts = outgoing_sms_parts(message)

        try:
            cfgs = []
            missing_parts = []

            if reader.SMS_DRY_RUN:
                missing_parts = list(parts)
            else:
                for part in parts:
                    existing = client.find_outbox_message(recipient, part)
                    if existing is not None:
                        cfgs.append(existing["cfg"])
                        log.info(
                            "Cudy SMS %s-del findes allerede i Outbox (cfg=%s); "
                            "springer dublet over",
                            message_id,
                            existing["cfg"],
                        )
                    else:
                        missing_parts.append(part)

            if missing_parts and not reader.SMS_DRY_RUN:
                # SMS Enable is infrastructure, not a per-message switch.
                # Keep it permanently enabled so both receive and send work
                # unattended, including after a Cudy reboot.
                client.ensure_sms_enabled()

            for part in missing_parts:
                if reader.SMS_DRY_RUN:
                    log.info(
                        "DRY RUN Cudy SMS %s til %s: %s",
                        message_id,
                        recipient,
                        part,
                    )
                    result = {
                        "accepted": True,
                        "cfg": "dry-run",
                        "sms_engine_enabled": True,
                    }
                else:
                    result = client.send_sms(recipient, part)

                cfgs.append(str(result.get("cfg") or ""))

            reader.complete_outgoing(message_id, "sent")
            reader.write_status(
                state="online",
                transport="cudy",
                capability=CAPABILITY,
                sms_engine="enabled",
                last_sent_sms_at=reader.utc_iso(),
                last_sent_recipient=recipient,
                last_error=None,
            )
            log.info(
                "Udgående Cudy SMS %s accepteret til %s (%s del(e), cfg=%s)",
                message_id,
                recipient,
                len(parts),
                ",".join(cfgs),
            )
        except (CudyError, ValueError) as exc:
            # Parts carry a stable queue-job marker. On retry, already accepted
            # parts are found in Outbox and skipped instead of being duplicated.
            reader.complete_outgoing(
                message_id,
                "failed",
                error=str(exc),
                retry=True,
            )
            reader.write_status(
                state="degraded",
                transport="cudy",
                capability=CAPABILITY,
                last_error=str(exc)[:240],
            )
            log.exception("Udgående Cudy SMS %s fejlede", message_id)
            return

        processed += 1


def run():
    retry_seconds = 2
    # Source namespace remains stable across host/container changes.
    reader.MODEM_DEVICE = os.getenv("CUDY_BASE_URL", "http://192.168.10.1").rstrip("/")
    while reader.running:
        try:
            reader.write_status(state="connecting", transport="cudy", capability=CAPABILITY, last_error=None)
            client = CudyClient()
            probe = client.probe()
            if "READY" not in probe["sim"]:
                raise CudyError("Routerens SIM er ikke klar; kontrollér SIM-kort og PIN i Cudy")

            # Physical LT300 testing confirmed that the stock SMS service must
            # stay enabled. Self-heal it on startup so no browser action is
            # required after a router/container restart.
            client.ensure_sms_enabled()

            retry_seconds = 2
            next_check = 0.0
            next_sms_engine_check = 0.0
            network = probe["network"]
            strength = probe["signal"]
            while reader.running:
                now = time.monotonic()
                if now >= next_check:
                    network = client.command("AT+CEREG?")
                    if "ERROR" in network:
                        network = client.command("AT+CREG?")
                    strength = client.command("AT+CSQ")
                    reader.write_status(storage=read_storage(client))
                    next_check = now + 30

                if now >= next_sms_engine_check:
                    if not client.sms_enabled():
                        log.warning(
                            "Cudy SMS Enable var slået fra; aktiverer automatisk"
                        )
                        client.set_sms_enabled(True)
                    next_sms_engine_check = now + SMS_ENGINE_CHECK_SECONDS

                reader.write_status(state="online" if registered(network) else "degraded", network=network.strip(), signal=strength.strip(), sim=probe["sim"].strip(), transport="cudy", capability=CAPABILITY, sms_engine="enabled", last_error=None if registered(network) else "Routeren er ikke registreret på mobilnettet")
                for message in read_inbox(client):
                    try:
                        import_message(client, message)
                    except Exception:
                        log.exception("SMS fra Cudy beholdes til et nyt importforsøg")
                        reader.write_status(state="degraded", last_error="SMS kunne ikke importeres; den beholdes på routeren")
                process_outbox(client)
                time.sleep(POLL_SECONDS)
        except Exception as exc:
            log.error("Cudy-forbindelsen fejlede: %s", exc)
            reader.write_status(state="offline", transport="cudy", capability=CAPABILITY, last_error=str(exc)[:240])
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 60)


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        # Diagnostic only: no SMS deletion or WhatsApp transmission.
        print(json.dumps(CudyClient().probe(), ensure_ascii=False, indent=2))
    else:
        signal.signal(signal.SIGTERM, reader.stop)
        signal.signal(signal.SIGINT, reader.stop)
        run()