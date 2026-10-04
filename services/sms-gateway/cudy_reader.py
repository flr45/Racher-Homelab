"""Read SMS through the LT300's stock LuCI AT form; reuse the USB alarm flow."""
import json
import logging
import os
import re
import signal
import time

import modem_reader_sbr as pager
from cudy_client import CudyClient, CudyError

reader = pager.reader
log = logging.getLogger("sms-cudy-reader")
POLL_SECONDS = max(2.0, float(os.getenv("CUDY_POLL_SECONDS", "5")))
SEND_ENABLED = os.getenv("CUDY_SMS_SEND_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
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
        body = message["body"]

        try:
            if reader.SMS_DRY_RUN:
                log.info("DRY RUN Cudy SMS %s til %s: %s", message_id, recipient, body)
                result = {"accepted": True, "cfg": "dry-run", "restore_error": None}
            else:
                result = client.send_sms(recipient, body)

            reader.complete_outgoing(message_id, "sent")
            status = {
                "state": "online",
                "transport": "cudy",
                "capability": CAPABILITY,
                "last_sent_sms_at": reader.utc_iso(),
                "last_sent_recipient": recipient,
                "last_error": None,
            }
            if result.get("restore_error"):
                status["state"] = "degraded"
                status["last_error"] = (
                    "SMS blev accepteret, men Cudy SMS Enable kunne ikke "
                    "gendannes: " + str(result["restore_error"])
                )[:240]
            reader.write_status(**status)
            log.info(
                "Udgående Cudy SMS %s accepteret til %s (cfg=%s)",
                message_id,
                recipient,
                result.get("cfg"),
            )
        except (CudyError, ValueError) as exc:
            # Never auto-retry an ambiguous Cudy send. send_sms() reconciles
            # Outbox first, but a very late router write could otherwise make a
            # retry produce a duplicate SMS.
            reader.complete_outgoing(
                message_id,
                "failed",
                error=str(exc),
                retry=False,
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
            retry_seconds = 2
            next_check = 0.0
            network = probe["network"]
            strength = probe["signal"]
            while reader.running:
                if time.monotonic() >= next_check:
                    network = client.command("AT+CEREG?")
                    if "ERROR" in network:
                        network = client.command("AT+CREG?")
                    strength = client.command("AT+CSQ")
                    reader.write_status(storage=read_storage(client))
                    next_check = time.monotonic() + 30
                reader.write_status(state="online" if registered(network) else "degraded", network=network.strip(), signal=strength.strip(), sim=probe["sim"].strip(), transport="cudy", capability=CAPABILITY, last_error=None if registered(network) else "Routeren er ikke registreret på mobilnettet")
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