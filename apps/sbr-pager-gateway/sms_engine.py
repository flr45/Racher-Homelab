from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable

import serial

from policy import normalize_phone, should_ignore_message
from sms_pdu import parse_cmgl_response
from storage import (
    add_event,
    get_setting,
    is_sender_allowed,
    set_message_status,
    set_setting,
    store_inbound_message,
)

StatusCallback = Callable[[str, str], None]
MessageCallback = Callable[[dict], None]


class SmsModemEngine:
    """Owns one serial modem port and imports SMS messages into local SQLite."""

    def __init__(
        self,
        port_name: str,
        *,
        baudrate: int = 115200,
        poll_seconds: float = 2.0,
        on_status: StatusCallback | None = None,
        on_message: MessageCallback | None = None,
    ) -> None:
        self.port_name = port_name
        self.baudrate = baudrate
        self.poll_seconds = max(1.0, poll_seconds)
        self.on_status = on_status
        self.on_message = on_message
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _status(self, state: str, detail: str) -> None:
        if self.on_status:
            self.on_status(state, detail)

    def run(self) -> None:
        retry_seconds = 2
        while not self._stop.is_set():
            try:
                self._status("connecting", f"Forbinder til {self.port_name}")
                with serial.Serial(
                    port=self.port_name,
                    baudrate=self.baudrate,
                    timeout=0.30,
                    write_timeout=2,
                ) as port:
                    try:
                        port.dtr = False
                        port.rts = False
                    except (OSError, ValueError):
                        pass

                    self._initialize(port)
                    retry_seconds = 2
                    self._status("online", f"SMS-modem online på {self.port_name}")
                    add_event("info", "modem_online", f"SMS-modem online på {self.port_name}")

                    self._bootstrap_existing_sms(port)

                    while not self._stop.is_set():
                        self._poll_once(port)
                        self._stop.wait(self.poll_seconds)

            except (serial.SerialException, OSError, TimeoutError, RuntimeError, ValueError) as exc:
                detail = str(exc) or exc.__class__.__name__
                self._status("offline", detail)
                add_event("error", "modem_error", detail[:1000])
                if self._stop.wait(retry_seconds):
                    break
                retry_seconds = min(retry_seconds * 2, 60)

        self._status("stopped", "Gateway stoppet")

    def _initialize(self, port: serial.Serial) -> None:
        for value in ("AT", "ATE0", "AT+CMGF=0", 'AT+CPMS="SM","SM","SM"'):
            response = self._command(port, value, timeout=8)
            if "ERROR" in response or "+CME ERROR:" in response or "+CMS ERROR:" in response:
                raise RuntimeError(f"Modemmet afviste {value}: {response.strip()}")

    def _bootstrap_existing_sms(self, port: serial.Serial) -> None:
        """Archive and clear pre-existing unread SMS without forwarding them."""
        key = f"modem_bootstrap_complete:{self.port_name}"
        if get_setting(key, "0") == "1":
            return

        response = self._command(port, "AT+CMGL=0", timeout=15)
        messages = parse_cmgl_response(response)
        skipped = 0
        for message in messages:
            source_id = self._source_id(message)
            store_inbound_message(
                source_id=source_id,
                sender=message["sender"],
                body=message["body"],
                received_at=message["timestamp"],
                modem_port=self.port_name,
                processing_status="bootstrap_skipped",
            )
            self._delete_message(port, message)
            skipped += 1

        set_setting(key, "1")
        if skipped:
            add_event(
                "info",
                "bootstrap",
                f"{skipped} eksisterende SMS blev arkiveret uden videresendelse",
            )
            self._status(
                "online",
                f"Modem online · {skipped} gamle SMS arkiveret uden videresendelse",
            )

    def _poll_once(self, port: serial.Serial) -> None:
        response = self._command(port, "AT+CMGL=0", timeout=15)
        messages = parse_cmgl_response(response)
        for message in messages:
            source_id = self._source_id(message)
            message_id, inserted = store_inbound_message(
                source_id=source_id,
                sender=message["sender"],
                body=message["body"],
                received_at=message["timestamp"],
                modem_port=self.port_name,
                processing_status="received",
            )

            # SQLite is the durability boundary. The SMS is only removed from
            # the SIM after the local copy is known to exist.
            self._delete_message(port, message)

            if inserted:
                final_status, accepted = self._classify_message(message)
                set_message_status(message_id, final_status, accepted=accepted)
                add_event(
                    "info",
                    "sms_received",
                    f"SMS {message_id} fra {message['sender']} → {final_status}",
                )
                if self.on_message:
                    event = dict(message)
                    event["database_id"] = message_id
                    event["processing_status"] = final_status
                    event["accepted"] = accepted
                    self.on_message(event)

        self._status("online", f"SMS-modem online på {self.port_name}")

    @staticmethod
    def _classify_message(message: dict) -> tuple[str, bool]:
        body = str(message.get("body") or "")
        if should_ignore_message(body):
            return "ignored_command", False

        try:
            sender = normalize_phone(str(message.get("sender") or ""))
        except ValueError:
            return "rejected_sender", False

        if is_sender_allowed(sender):
            return "accepted_pending_whatsapp", True
        return "rejected_sender", False

    def _delete_message(self, port: serial.Serial, message: dict) -> None:
        for index in message["indices"]:
            response = self._command(port, f"AT+CMGD={index}", timeout=8)
            if "ERROR" in response or "+CME ERROR:" in response or "+CMS ERROR:" in response:
                raise RuntimeError(f"Kunne ikke slette SMS {index} fra SIM")

    def _source_id(self, message: dict) -> str:
        stable = "|".join(
            [
                self.port_name,
                ",".join(str(index) for index in message["indices"]),
                message.get("timestamp") or "",
                message.get("sender") or "",
                message.get("body") or "",
            ]
        )
        return "sbr-sms:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()

    @staticmethod
    def _command(
        port: serial.Serial,
        value: str,
        *,
        timeout: float = 8,
        expected: str = "\r\nOK\r\n",
    ) -> str:
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
                if any(marker in text for marker in ("\r\nERROR\r\n", "+CME ERROR:", "+CMS ERROR:")):
                    return text
            else:
                time.sleep(0.03)

        raise TimeoutError(f"Modemmet svarede ikke på {value}")
