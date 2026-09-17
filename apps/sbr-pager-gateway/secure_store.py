from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from storage import data_dir


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI er kun tilgængelig på Windows")


def _make_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def protect_bytes(data: bytes) -> bytes:
    _require_windows()
    in_blob, in_buffer = _make_blob(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "SBR Pager Gateway",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    _ = in_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def unprotect_bytes(data: bytes) -> bytes:
    _require_windows()
    in_blob, in_buffer = _make_blob(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    _ = in_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def credential_path(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in {"-", "_"})
    if not safe:
        raise ValueError("Ugyldigt credential-navn")
    return data_dir() / f"{safe}.dpapi"


def save_json(name: str, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = credential_path(name)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(protect_bytes(raw))
    temp.replace(path)


def load_json(name: str) -> dict | None:
    path = credential_path(name)
    if not path.exists():
        return None
    raw = unprotect_bytes(path.read_bytes())
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def delete(name: str) -> None:
    path = credential_path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
