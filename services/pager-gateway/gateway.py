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

RIC_RE = re.compile(r"\b(?:RIC|CAP(?:CODE)?)\s*[:=]?\s*(\d{4,10})\b", re.I)
BAUD_RE = re.compile(r"\b(?:POCSAG[- ]?)?(512|1200|2400)\b", re.I)
FUNCTION_RE = re.compile(r"\b(?:FUNC(?:TION)?|F)\s*[:=]?\s*([0-3A-D])\b", re.I)


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

    ric_match = RIC_RE.search(raw)
    baud_match = BAUD_RE.search(raw)
    function_match = FUNCTION_RE.search(raw)

    message = raw
    # PDL's current Linux build emits human-readable decoded lines. Keep the
    # complete raw line, but strip a common "MESSAGE:" prefix when present.
    message_match = re.search(r"\bMESSAGE\s*[:=]\s*(.+)$", raw, re.I)
    if message_match:
        message = message_match.group(1).strip()

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
            data={
                "token": app_token,
                "user": user_key,
                "title": title,
                "message": message,
            },
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
                    # On startup we only want new radio traffic, not replay an old log.
                    handle.seek(0, os.SEEK_END)
                    self._status = "running"
                    self._error = None

                line = handle.readline()
                if line:
                    self.on_line(line)
                    continue

                # Handle rotation/truncation.
                try:
                    if path.stat().st_size < handle.tell():
                        handle.close()
                        handle = None
                except FileNotFoundError:
                    handle.close()
                    handle = None

                time.sleep(0.2)
        except Exception as exc:  # keep diagnostics visible in the web UI
            self._status = "error"
            self._error = str(exc)
        finally:
            if handle:
                handle.close()
            if self._status != "error":
                self._status = "stopped"
