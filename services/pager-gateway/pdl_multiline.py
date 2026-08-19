from __future__ import annotations

import os
import time
import types
from pathlib import Path
from typing import Any

from gateway import PDW_POCSAG_RE


DEFAULT_FLUSH_DELAY_SECONDS = 0.2
_MIN_CONTINUATION_INDENT = 8


def _pocsag_match(line: str):
    return PDW_POCSAG_RE.match(str(line or "").strip())


def is_alpha_header(line: str) -> bool:
    """Return whether a physical PDL line starts an ALPHA POCSAG record."""
    match = _pocsag_match(line)
    return bool(match and "ALPHA" in str(match.group("type") or "").upper())


def is_pocsag_header(line: str) -> bool:
    return _pocsag_match(line) is not None


def is_wrapped_continuation(line: str) -> bool:
    """Recognize PDL's visually-indented continuation rows.

    PDL aligns wrapped ALPHA text below the payload column. Normal POCSAG rows
    can also have leading whitespace, so a continuation must be indented and
    must not itself match the complete PDW/POCSAG header format.
    """
    raw = str(line or "")
    if not raw.strip() or is_pocsag_header(raw):
        return False
    leading = len(raw) - len(raw.lstrip(" \t"))
    return leading >= _MIN_CONTINUATION_INDENT


def join_wrapped_pdl_lines(lines: list[str]) -> str:
    """Join one PDL header and its wrapped payload rows into one logical line."""
    if not lines:
        return ""
    head = str(lines[0]).rstrip("\r\n").rstrip()
    tails = [str(line).strip() for line in lines[1:] if str(line).strip()]
    return " ".join([head, *tails]) if tails else head


def _save_cursor_at(source: Any, path: Path, handle: Any, offset: int) -> None:
    """Persist a committed offset without disturbing the tailer's read position."""
    current = handle.tell()
    try:
        handle.seek(offset, os.SEEK_SET)
        source._save_cursor(path, handle)
    finally:
        handle.seek(current, os.SEEK_SET)


def _multiline_run(source: Any) -> None:
    source._status = "waiting"
    current_path: str | None = None
    handle = None
    pending: list[str] = []
    pending_end: int | None = None
    pending_updated = 0.0
    flush_delay = float(getattr(source, "_pdl_multiline_flush_delay", DEFAULT_FLUSH_DELAY_SECONDS))

    def flush_pending(path: Path) -> None:
        nonlocal pending, pending_end, pending_updated
        if not pending or handle is None or pending_end is None:
            return
        logical_line = join_wrapped_pdl_lines(pending)
        source.on_line(logical_line)
        _save_cursor_at(source, path, handle, pending_end)
        pending = []
        pending_end = None
        pending_updated = 0.0

    try:
        while not source._stop.is_set():
            wanted_path = source.get_path()
            if wanted_path != current_path:
                if handle:
                    flush_pending(Path(current_path))
                    handle.close()
                    handle = None
                current_path = wanted_path

            path = Path(current_path)
            if not path.exists():
                source._status = "waiting"
                source._error = f"Venter på {path}"
                time.sleep(1)
                continue

            if handle is None:
                handle = path.open("r", encoding="utf-8", errors="replace")
                source._resume_position(path, handle)
                source._save_cursor(path, handle)
                source._status = "running"
                source._error = None

            line = handle.readline()
            if line:
                line_end = handle.tell()

                if is_alpha_header(line):
                    flush_pending(path)
                    pending = [line]
                    pending_end = line_end
                    pending_updated = time.monotonic()
                    continue

                if pending and is_wrapped_continuation(line):
                    pending.append(line)
                    pending_end = line_end
                    pending_updated = time.monotonic()
                    continue

                # Any other complete decoder row is a boundary for a pending
                # ALPHA record. Commit the full ALPHA first, then this row.
                flush_pending(path)
                source.on_line(line)
                source._save_cursor(path, handle)
                continue

            if pending and (time.monotonic() - pending_updated) >= flush_delay:
                flush_pending(path)

            try:
                replaced = not source._same_file(path, handle)
                truncated = path.stat().st_size < handle.tell()
                if replaced or truncated:
                    flush_pending(path)
                    handle.close()
                    handle = None
            except FileNotFoundError:
                flush_pending(path)
                handle.close()
                handle = None

            time.sleep(min(0.05, max(0.01, flush_delay / 4)))
    except Exception as exc:
        source._status = "error"
        source._error = str(exc)
    finally:
        if handle:
            try:
                flush_pending(Path(current_path))
            finally:
                handle.close()
        if source._status != "error":
            source._status = "stopped"


def install_pdl_multiline_tail(source: Any, flush_delay_seconds: float = DEFAULT_FLUSH_DELAY_SECONDS) -> Any:
    """Install crash-safe multiline PDL assembly on an unstarted FileTailSource."""
    if getattr(source, "_pdl_multiline_installed", False):
        return source
    source._pdl_multiline_flush_delay = max(0.02, min(float(flush_delay_seconds), 1.0))
    source._run = types.MethodType(_multiline_run, source)
    source._pdl_multiline_installed = True
    return source
