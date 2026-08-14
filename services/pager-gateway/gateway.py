from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests


STATION_MARKERS = {
    "A": "Slagelse",
    "S": "Sorø",
    "K": "Korsør",
    "L": "Skælskør",
    "R": "Ruds Vedby",
}

RIC_RE = re.compile(r"\b(?:RIC|CAP(?:CODE)?|ADDRESS)\s*[:=]?\s*(\d{4,10})\b", re.I)
BAUD_RE = re.compile(r"\b(?:POCSAG[- ]?)?(512|1200|2400)\b", re.I)
FUNCTION_RE = re.compile(r"\b(?:FUNC(?:TION)?|F)\s*[:=]?\s*([0-4A-D])\b", re.I)
PUBLIC_RIC_FIELD_RE = re.compile(r"\b(?:RIC|CAP(?:CODE)?|ADDRESS)\s*[:=]?\s*\d{4,10}\b", re.I)
PDW_POCSAG_RE = re.compile(
    r"^\s*(?P<ric>\d{4,10})\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2})\s+"
    r"(?P<date>\d{2}-\d{2}-\d{2,4})\s+"
    r"POCSAG(?:-(?P<function>[1-4]))?\s+"
    r"(?P<type>\S+)\s+"
    r"(?P<baud>512|1200|2400)\s+"
    r"(?P<message>.+)$",
    re.I,
)


@dataclass
class PagerEvent:
    message: str
    raw_line: str
    source: str
    protocol: str = "POCSAG"
    baud: int | None = None
    ric: str | None = None
    function: str | None = None
    station: str | None = None
    received_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        if not data["received_at"]:
            data["received_at"] = datetime.now(timezone.utc).isoformat()
        return data


def public_message(text: str) -> str:
    """Return user-facing alarm text with decoder/capcode metadata removed.

    The raw decoder line is retained separately for admins. This function is used
    for every outbound notification so RIC/capcode never becomes user-facing data.
    """
    value = str(text or "").strip()
    message_match = re.search(r"\bMESSAGE\s*[:=]\s*(.+)$", value, re.I)
    if message_match:
        value = message_match.group(1).strip()
    value = PUBLIC_RIC_FIELD_RE.sub("", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" -|:;")
    return value


def detect_station(text: str) -> str | None:
    upper = text.upper()
    for marker, station in STATION_MARKERS.items():
        if f"({marker})" in upper:
            return station
    return None


def parse_pdl_line(line: str, source: str = "pdl") -> PagerEvent | None:
    raw = line.strip()
    if not raw:
        return None

    pdw_match = PDW_POCSAG_RE.match(raw)
    if pdw_match:
        message = public_message(pdw_match.group("message"))
        return PagerEvent(
            message=message,
            raw_line=raw,
            source=source,
            protocol="POCSAG",
            baud=int(pdw_match.group("baud")),
            ric=pdw_match.group("ric"),
            function=pdw_match.group("function"),
            station=detect_station(message),
        )

    ric_match = RIC_RE.search(raw)
    baud_match = BAUD_RE.search(raw)
    function_match = FUNCTION_RE.search(raw)

    message = public_message(raw)
    return PagerEvent(
        message=message,
        raw_line=raw,
        source=source,
        baud=int(baud_match.group(1)) if baud_match else None,
        ric=ric_match.group(1) if ric_match else None,
        function=function_match.group(1) if function_match else None,
        station=detect_station(message),
    )


class PushoverClient:
    endpoint = "https://api.pushover.net/1/messages.json"

    def send(self, app_token: str, user_key: str, title: str, message: str) -> None:
        if not app_token or not user_key:
            raise ValueError("Pushover app token eller user key mangler")
        response = requests.post(
            self.endpoint,
            data={"token": app_token, "user": user_key, "title": title, "message": public_message(message)},
            timeout=10,
        )
        response.raise_for_status()


class FileTailSource:
    """Tail a PDL output/log file and forward each new decoded line."""

    def __init__(self, get_path: Callable[[], str], on_line: Callable[[str], None]) -> None:
        self.get_path = get_path
        self.on_line = on_line
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status = "stopped"
        self._error: str | None = None

    @property
    def status(self) -> dict[str, str | None]:
        return {"state": self._status, "error": self._error}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pdl-file-tail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self._status = "waiting"
        current_path = None
        handle = None
        try:
            while not self._stop.is_set():
                wanted_path = self.get_path()
                if wanted_path != current_path:
                    if handle:
                        handle.close()
                        handle = None
                    current_path = wanted_path

                path = Path(current_path)
                if not path.exists():
                    self._status = "waiting"
                    self._error = f"Venter på {path}"
                    time.sleep(1)
                    continue

                if handle is None:
                    handle = path.open("r", encoding="utf-8", errors="replace")
                    handle.seek(0, os.SEEK_END)
                    self._status = "running"
                    self._error = None

                line = handle.readline()
                if line:
                    self.on_line(line)
                    continue

                try:
                    if path.stat().st_size < handle.tell():
                        handle.close()
                        handle = None
                except FileNotFoundError:
                    handle.close()
                    handle = None

                time.sleep(0.2)
        except Exception as exc:
            self._status = "error"
            self._error = str(exc)
        finally:
            if handle:
                handle.close()
            if self._status != "error":
                self._status = "stopped"
