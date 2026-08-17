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

# Danish ISO 646 / DS 2089 replaces the ASCII bracket characters with ÆØÅ/æøå.
# PDL exposes the raw 7-bit values. Translation must only be applied to alpha
# payloads: applying it to numeric/tone decoder output turns e.g. `40]04` into
# the misleading `40Å04` seen in operator notifications.
POCSAG_DANISH_TRANSLATION = str.maketrans({
    "[": "Æ",
    "\\": "Ø",
    "]": "Å",
    "{": "æ",
    "|": "ø",
    "}": "å",
})

_DOUBLE_UNKNOWN_SEPARATOR_RE = re.compile(r"(?<=\S)\?{2,}(?=\S)")
_ALPHA_WORD_RE = re.compile(r"[A-Za-zÆØÅæøå]{2,}")
_DECODER_CODE_RE = re.compile(r"^[0-9A-Fa-f*+\-?/\\\[\]{}|ÆØÅæøå\s]{3,120}$")


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
    decoder_noise_reason: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if not data["received_at"]:
            data["received_at"] = datetime.now(timezone.utc).isoformat()
        return data


def public_message(text: str) -> str:
    """Return readable user-facing alarm text with decoder metadata removed.

    Raw decoder input is retained separately in ``raw_line``. Repeated question
    marks emitted as field separators by the decoder are rendered as a neutral
    middle dot; a single `?` is preserved because it can represent a genuinely
    undecodable character and therefore remains useful diagnostic information.
    """
    value = str(text or "").strip()
    message_match = re.search(r"\bMESSAGE\s*[:=]\s*(.+)$", value, re.I)
    if message_match:
        value = message_match.group(1).strip()
    value = PUBLIC_RIC_FIELD_RE.sub("", value)
    value = _DOUBLE_UNKNOWN_SEPARATOR_RE.sub(" · ", value)
    value = re.sub(r"\s*·\s*", " · ", value)
    value = re.sub(r"(?:\s*·\s*){2,}", " · ", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" -|:;·")
    return value


def decode_pocsag_danish_charset(text: str) -> str:
    """Translate Danish 7-bit pager characters to Unicode ÆØÅ/æøå."""
    return str(text or "").translate(POCSAG_DANISH_TRANSLATION)


def _looks_alpha_payload(message: str, payload_type: str | None = None) -> bool:
    kind = str(payload_type or "").upper()
    if kind:
        return "ALPHA" in kind
    return bool(_ALPHA_WORD_RE.search(str(message or "")))


def _message_for_source(text: str, source: str, payload_type: str | None = None) -> str:
    message = public_message(text)
    if str(source or "").lower().startswith("pdl") and _looks_alpha_payload(message, payload_type):
        message = decode_pocsag_danish_charset(message)
        message = public_message(message)
    return message


def decoder_noise_reason(message: str, source: str, payload_type: str | None = None) -> str | None:
    """Classify obvious decoder-only output without deleting its raw line.

    This is deliberately conservative and only applies to the live PDL source.
    Simulator/import text remains untouched. The caller stores the event but
    marks it delivery-ineligible, keeping diagnostics while protecting Pushover.
    """
    if not str(source or "").lower().startswith("pdl"):
        return None

    value = str(message or "").strip()
    kind = str(payload_type or "").upper()
    if kind and "ALPHA" not in kind:
        return "decoder-non-alpha"
    if not value:
        return "decoder-empty"

    words = _ALPHA_WORD_RE.findall(value)
    if not words and _DECODER_CODE_RE.fullmatch(value):
        return "decoder-code"

    # Suffix fragments such as "førerhus, spredt sig" can be emitted as their
    # own line when a reception breaks mid-message. Keep them in history but do
    # not turn a short, lowercase tail into a standalone alarm notification.
    if (
        len(value) <= 48
        and value[:1].islower()
        and len(words) <= 6
        and not re.search(r"\b(?:BRAND|ALARM|ISL|VSBV|ØF|VCT)\b", value, re.I)
    ):
        return "decoder-fragment"
    return None


def detect_station(text: str) -> str | None:
    upper = text.upper()
    for marker, station in STATION_MARKERS.items():
        if f"({marker})" in upper:
            return station
    return None


def _pdw_received_at(date_value: str, time_value: str) -> str:
    """Preserve the local timestamp carried by PDW output.

    PDW logs do not include a timezone. Returning a timezone-naive ISO timestamp
    preserves the decoder's local wall-clock value for history/replay while still
    allowing reliable time deltas between two PDW rows from the same log.
    """
    raw = f"{date_value} {time_value}"
    for date_format in ("%d-%m-%y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, date_format).isoformat()
        except ValueError:
            continue
    return ""


def parse_pdl_line(line: str, source: str = "pdl") -> PagerEvent | None:
    raw = line.strip()
    if not raw:
        return None

    pdw_match = PDW_POCSAG_RE.match(raw)
    if pdw_match:
        payload_type = pdw_match.group("type")
        message = _message_for_source(pdw_match.group("message"), source, payload_type)
        return PagerEvent(
            message=message,
            raw_line=raw,
            source=source,
            protocol="POCSAG",
            baud=int(pdw_match.group("baud")),
            ric=pdw_match.group("ric"),
            function=pdw_match.group("function"),
            station=detect_station(message),
            received_at=_pdw_received_at(pdw_match.group("date"), pdw_match.group("time")),
            decoder_noise_reason=decoder_noise_reason(message, source, payload_type),
        )

    ric_match = RIC_RE.search(raw)
    baud_match = BAUD_RE.search(raw)
    function_match = FUNCTION_RE.search(raw)

    message = _message_for_source(raw, source)
    return PagerEvent(
        message=message,
        raw_line=raw,
        source=source,
        baud=int(baud_match.group(1)) if baud_match else None,
        ric=ric_match.group(1) if ric_match else None,
        function=function_match.group(1) if function_match else None,
        station=detect_station(message),
        decoder_noise_reason=decoder_noise_reason(message, source),
    )


class PushoverClient:
    endpoint = "https://api.pushover.net/1/messages.json"

    def send(self, app_token: str, user_key: str, title: str, message: str) -> None:
        if not app_token or not user_key:
            raise ValueError("Pushover app token eller user key mangler")
        requested_title = str(title or "").strip()
        display_title = "Lind Foto" if requested_title in {"", "Racher Pager Gateway"} else requested_title
        response = requests.post(
            self.endpoint,
            data={"token": app_token, "user": user_key, "title": display_title, "message": public_message(message)},
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

    @staticmethod
    def _same_file(path: Path, handle) -> bool:
        """Return whether ``path`` still names the file currently held open.

        A normal log rotation can replace the path with a new inode whose size is
        larger than the old file. Size-only truncation detection would then leave
        the tailer stuck on the unlinked old inode forever.
        """
        try:
            path_stat = path.stat()
            open_stat = os.fstat(handle.fileno())
        except (FileNotFoundError, OSError, ValueError):
            return False
        return path_stat.st_dev == open_stat.st_dev and path_stat.st_ino == open_stat.st_ino

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
                    if not self._same_file(path, handle) or path.stat().st_size < handle.tell():
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
