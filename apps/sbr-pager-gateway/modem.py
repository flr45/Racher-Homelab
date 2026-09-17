from __future__ import annotations

import re
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


BAUD_CANDIDATES = (115200, 57600, 38400, 19200, 9600)


@dataclass(slots=True)
class ModemInfo:
    port: str
    description: str
    manufacturer: str = ""
    model: str = ""
    sim_status: str = "Ukendt"
    network_status: str = "Ukendt"
    signal_rssi: int | None = None
    baudrate: int = 115200

    @property
    def signal_percent(self) -> int | None:
        if self.signal_rssi is None or self.signal_rssi == 99:
            return None
        value = max(0, min(31, self.signal_rssi))
        return round(value / 31 * 100)


def _read_response(port: serial.Serial, timeout: float = 1.5) -> str:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            data.extend(chunk)
            text = data.decode("ascii", errors="replace")
            if any(
                marker in text
                for marker in ("\r\nOK\r\n", "\r\nERROR\r\n", "+CME ERROR:", "+CMS ERROR:")
            ):
                return text
        else:
            time.sleep(0.03)
    return data.decode("ascii", errors="replace")


def at_command(port: serial.Serial, command: str, timeout: float = 1.5) -> str:
    try:
        port.reset_input_buffer()
    except (OSError, serial.SerialException):
        pass
    port.write((command + "\r").encode("ascii"))
    port.flush()
    return _read_response(port, timeout=timeout)


def _clean_single_value(response: str, command: str) -> str:
    lines = []
    for raw in response.replace("\r", "").split("\n"):
        value = raw.strip()
        if not value or value in {"OK", "ERROR", command}:
            continue
        lines.append(value)
    return " ".join(lines).strip()


def _parse_csq(response: str) -> int | None:
    match = re.search(r"\+CSQ:\s*(\d+)", response)
    return int(match.group(1)) if match else None


def _probe_at(device: str, baudrate: int) -> serial.Serial | None:
    try:
        port = serial.Serial(
            port=device,
            baudrate=baudrate,
            timeout=0.25,
            write_timeout=1.5,
        )
    except (serial.SerialException, OSError, ValueError):
        return None

    try:
        # Some USB modems need a short settle time after opening the virtual
        # COM port. Do not force DTR/RTS low here: a number of GSM dongles use
        # those lines to wake or reset the command channel.
        time.sleep(0.15)
        for _ in range(2):
            answer = at_command(port, "AT", timeout=1.8)
            if "OK" in answer:
                return port
            time.sleep(0.10)
    except (serial.SerialException, OSError, TimeoutError):
        pass

    try:
        port.close()
    except OSError:
        pass
    return None


def probe_port(device: str, description: str = "") -> ModemInfo | None:
    for baudrate in BAUD_CANDIDATES:
        port = _probe_at(device, baudrate)
        if port is None:
            continue

        try:
            at_command(port, "ATE0", timeout=1.2)
            manufacturer = _clean_single_value(
                at_command(port, "AT+CGMI", timeout=2.0), "AT+CGMI"
            )
            model = _clean_single_value(
                at_command(port, "AT+CGMM", timeout=2.0), "AT+CGMM"
            )
            sim = _clean_single_value(
                at_command(port, "AT+CPIN?", timeout=2.0), "AT+CPIN?"
            )
            network = _clean_single_value(
                at_command(port, "AT+CREG?", timeout=2.0), "AT+CREG?"
            )
            signal_response = at_command(port, "AT+CSQ", timeout=2.0)

            return ModemInfo(
                port=device,
                description=description,
                manufacturer=manufacturer or "Ukendt",
                model=model or "Ukendt",
                sim_status=sim or "Ukendt",
                network_status=network or "Ukendt",
                signal_rssi=_parse_csq(signal_response),
                baudrate=baudrate,
            )
        except (serial.SerialException, OSError, TimeoutError):
            # We already got a valid AT response, so keep the port even if an
            # optional information command is unsupported.
            return ModemInfo(
                port=device,
                description=description,
                baudrate=baudrate,
            )
        finally:
            try:
                port.close()
            except OSError:
                pass

    return None


def available_serial_ports() -> list[tuple[str, str]]:
    return [
        (item.device, item.description or "Ukendt enhed")
        for item in list_ports.comports()
    ]


def discover_modems() -> list[ModemInfo]:
    found: list[ModemInfo] = []
    for item in list_ports.comports():
        result = probe_port(item.device, item.description or "")
        if result is not None:
            found.append(result)
    return found
