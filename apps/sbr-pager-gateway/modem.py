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
        return round(max(0, min(31, self.signal_rssi)) / 31 * 100)

def _read_response(port: serial.Serial, timeout: float = 1.5) -> str:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        try:
            waiting = port.in_waiting
        except (OSError, serial.SerialException):
            waiting = 0
        chunk = port.read(waiting or 1)
        if chunk:
            data.extend(chunk)
            text = data.decode("ascii", errors="replace")
            if re.search(r"(^|[\r\n])(OK|ERROR)([\r\n]|$)", text) or "+CME ERROR:" in text or "+CMS ERROR:" in text:
                return text
        else:
            time.sleep(0.03)
    return data.decode("ascii", errors="replace")

def at_command(port: serial.Serial, command: str, timeout: float = 1.5) -> str:
    port.write((command + "\r").encode("ascii"))
    port.flush()
    return _read_response(port, timeout)

def _clean_single_value(response: str, command: str) -> str:
    values=[]
    for raw in response.replace("\r","").split("\n"):
        value=raw.strip()
        if value and value not in {"OK","ERROR",command}:
            values.append(value)
    return " ".join(values).strip()

def _parse_csq(response: str) -> int | None:
    m=re.search(r"\+CSQ:\s*(\d+)", response)
    return int(m.group(1)) if m else None

def _open_port(device: str, baudrate: int) -> serial.Serial | None:
    try:
        port=serial.Serial(device, baudrate=baudrate, timeout=.30, write_timeout=2.0)
        # Huawei composite modems can take a moment after CreateFile().
        time.sleep(.35)
        try:
            port.reset_input_buffer()
            port.reset_output_buffer()
        except (OSError, serial.SerialException):
            pass
        return port
    except (serial.SerialException, OSError, ValueError):
        return None

def _probe_at(device: str, baudrate: int) -> serial.Serial | None:
    port=_open_port(device, baudrate)
    if port is None:
        return None
    try:
        # A few Huawei firmwares ignore the first command after opening.
        for command in ("AT", "AT", "ATE0", "AT"):
            answer=at_command(port, command, timeout=2.0)
            if "OK" in answer.upper():
                return port
            time.sleep(.15)
    except (serial.SerialException, OSError, TimeoutError):
        pass
    try: port.close()
    except OSError: pass
    return None

def probe_port(device: str, description: str = "") -> ModemInfo | None:
    for baudrate in BAUD_CANDIDATES:
        port=_probe_at(device, baudrate)
        if port is None:
            continue
        try:
            # Do not reject a valid AT port just because CMGF is unsupported at
            # probe time. The SMS engine performs its own full initialisation.
            at_command(port, "ATE0", 1.2)
            manufacturer=_clean_single_value(at_command(port,"AT+CGMI",2.0),"AT+CGMI")
            model=_clean_single_value(at_command(port,"AT+CGMM",2.0),"AT+CGMM")
            sim=_clean_single_value(at_command(port,"AT+CPIN?",2.0),"AT+CPIN?")
            network=_clean_single_value(at_command(port,"AT+CREG?",2.0),"AT+CREG?")
            signal=_parse_csq(at_command(port,"AT+CSQ",2.0))
            return ModemInfo(device, description, manufacturer or "Ukendt", model or "Ukendt",
                             sim or "Ukendt", network or "Ukendt", signal, baudrate)
        except (serial.SerialException, OSError, TimeoutError):
            return ModemInfo(device, description, baudrate=baudrate)
        finally:
            try: port.close()
            except OSError: pass
    return None

def available_serial_ports() -> list[tuple[str,str]]:
    ports=[(p.device,p.description or "Ukendt enhed") for p in list_ports.comports()]
    # Prefer actual modem/AT interfaces. Huawei "PC UI Interface" is commonly
    # diagnostic while "3G Modem"/"Modem" is the SMS-capable command port.
    def rank(item):
        text=item[1].lower()
        if "modem" in text: return 0
        if "application interface" in text: return 1
        if "pc ui" in text: return 3
        return 2
    return sorted(ports, key=rank)

def discover_modems() -> list[ModemInfo]:
    found=[]
    for device, description in available_serial_ports():
        result=probe_port(device, description)
        if result is not None:
            found.append(result)
    return found
