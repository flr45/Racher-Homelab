from __future__ import annotations

import re
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


@dataclass(slots=True)
class ModemInfo:
    port: str
    description: str
    manufacturer: str = ""
    model: str = ""
    sim_status: str = "Ukendt"
    network_status: str = "Ukendt"
    signal_rssi: int | None = None

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
            if "\r\nOK\r\n" in text or "\r\nERROR\r\n" in text:
                return text
        else:
            time.sleep(0.03)
    return data.decode("ascii", errors="replace")


def at_command(port: serial.Serial, command: str, timeout: float = 1.5) -> str:
    port.reset_input_buffer()
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


def probe_port(device: str, description: str = "") -> ModemInfo | None:
    try:
        with serial.Serial(
            port=device,
            baudrate=115200,
            timeout=0.15,
            write_timeout=1,
        ) as port:
            try:
                port.dtr = False
                port.rts = False
            except (OSError, ValueError):
                pass

            answer = at_command(port, "AT", timeout=1.2)
            if "OK" not in answer:
                return None

            at_command(port, "ATE0", timeout=1.0)
            manufacturer = _clean_single_value(at_command(port, "AT+CGMI"), "AT+CGMI")
            model = _clean_single_value(at_command(port, "AT+CGMM"), "AT+CGMM")
            sim = _clean_single_value(at_command(port, "AT+CPIN?"), "AT+CPIN?")
            network = _clean_single_value(at_command(port, "AT+CREG?"), "AT+CREG?")
            signal_response = at_command(port, "AT+CSQ")

            return ModemInfo(
                port=device,
                description=description,
                manufacturer=manufacturer or "Ukendt",
                model=model or "Ukendt",
                sim_status=sim or "Ukendt",
                network_status=network or "Ukendt",
                signal_rssi=_parse_csq(signal_response),
            )
    except (serial.SerialException, OSError, TimeoutError):
        return None


def discover_modems() -> list[ModemInfo]:
    found: list[ModemInfo] = []
    for item in list_ports.comports():
        result = probe_port(item.device, item.description or "")
        if result is not None:
            found.append(result)
    return found
