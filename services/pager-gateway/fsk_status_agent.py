#!/usr/bin/env python3
from __future__ import annotations

import configparser
import glob
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from storage import Storage


DB_PATH = os.getenv("PAGER_DB_PATH", "/var/lib/racher-pager/pager.db")
PDL_CONFIG_PATH = Path(os.getenv("PDL_CONFIG_PATH", "/var/lib/racher-pager/pdl/pdl.ini"))
INPUT_MODE = os.getenv("PDL_INPUT_MODE", "fsk-usb").strip().lower()
EXPLICIT_DEVICE = os.getenv("PDL_RS232_DEVICE", "").strip()
SERIAL_BITRATE = os.getenv("PDL_RS232_BITRATE", "19200").strip() or "19200"
SERIAL_FORMAT = "8N1"


def parse_udev_properties(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def udev_properties(path: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", str(path)],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    return parse_udev_properties(result.stdout or "")


def serial_candidates() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen_real: set[str] = set()
    patterns = ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*")
    for pattern in patterns:
        for raw in sorted(glob.glob(pattern)):
            path = Path(raw)
            try:
                if not path.exists():
                    continue
                real = str(path.resolve())
            except OSError:
                continue
            if real in seen_real:
                continue
            seen_real.add(real)
            props = udev_properties(path)
            haystack = " ".join(
                [
                    raw,
                    props.get("ID_VENDOR", ""),
                    props.get("ID_VENDOR_FROM_DATABASE", ""),
                    props.get("ID_MODEL", ""),
                    props.get("ID_SERIAL", ""),
                    props.get("ID_USB_DRIVER", ""),
                ]
            ).lower()
            score = 0
            if raw.startswith("/dev/serial/by-id/"):
                score += 40
            if real.startswith("/dev/ttyUSB"):
                score += 20
            if "ftdi" in haystack or "ft232" in haystack:
                score += 100
            found.append({"path": raw, "real": real, "props": props, "score": score})
    return found


def choose_device(candidates: list[dict[str, Any]], explicit: str = EXPLICIT_DEVICE) -> Optional[dict[str, Any]]:
    if explicit:
        explicit_path = Path(explicit)
        try:
            explicit_real = str(explicit_path.resolve()) if explicit_path.exists() else ""
        except OSError:
            explicit_real = ""
        for item in candidates:
            if item["path"] == explicit or (explicit_real and item["real"] == explicit_real):
                selected = dict(item)
                selected["score"] = int(selected.get("score", 0)) + 1000
                return selected
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("score", 0)))


def pdl_rs232_config() -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if PDL_CONFIG_PATH.exists():
        try:
            parser.read(PDL_CONFIG_PATH, encoding="utf-8")
        except (OSError, configparser.Error):
            pass
    section = parser["RS232"] if parser.has_section("RS232") else {}
    return {
        "decode_mode": str(section.get("DecodeMode", "0")),
        "port": str(section.get("Port", "1")),
        "bitrate": str(section.get("Bitrate", SERIAL_BITRATE)),
        "four_level": str(section.get("FourLevel", "0")),
    }


def pdl_owns_device(real_device: str) -> bool:
    if not real_device:
        return False
    target = Path(real_device)
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "pdl" not in cmdline.lower():
            continue
        fd_dir = proc / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in entries:
            try:
                if fd.resolve() == target:
                    return True
            except OSError:
                continue
    return False


def collect_status() -> dict[str, str]:
    candidates = serial_candidates()
    selected = choose_device(candidates)
    config = pdl_rs232_config()
    now = datetime.now(timezone.utc).isoformat()

    if selected:
        props = selected.get("props", {})
        vendor = props.get("ID_VENDOR_FROM_DATABASE") or props.get("ID_VENDOR") or ""
        model = props.get("ID_MODEL_FROM_DATABASE") or props.get("ID_MODEL") or ""
        serial = props.get("ID_SERIAL_SHORT") or props.get("ID_SERIAL") or ""
        driver = props.get("ID_USB_DRIVER") or ("ftdi_sio" if "ttyUSB" in selected["real"] else "")
        summary_bits = [part for part in (vendor, model, serial) if part]
        summary = " · ".join(summary_bits) or selected["path"]
        in_use = pdl_owns_device(selected["real"])
        return {
            "fsk_usb_connected": "1",
            "fsk_usb_devices": str(len(candidates)),
            "fsk_usb_device": str(selected["path"]),
            "fsk_usb_real_device": str(selected["real"]),
            "fsk_usb_summary": summary,
            "fsk_usb_vendor": vendor,
            "fsk_usb_model": model,
            "fsk_usb_serial": serial,
            "fsk_usb_driver": driver,
            "fsk_usb_input_mode": INPUT_MODE,
            "fsk_usb_serial_config": f"{config.get('bitrate') or SERIAL_BITRATE} {SERIAL_FORMAT}",
            "fsk_usb_decode_mode": config.get("decode_mode", "0"),
            "fsk_usb_pdl_port": config.get("port", "1"),
            "fsk_usb_four_level": config.get("four_level", "0"),
            "fsk_usb_pdl_in_use": "1" if in_use else "0",
            "fsk_usb_last_seen": now,
        }

    return {
        "fsk_usb_connected": "0",
        "fsk_usb_devices": "0",
        "fsk_usb_device": EXPLICIT_DEVICE,
        "fsk_usb_real_device": "",
        "fsk_usb_summary": "FSK-USB ikke fundet",
        "fsk_usb_vendor": "",
        "fsk_usb_model": "",
        "fsk_usb_serial": "",
        "fsk_usb_driver": "",
        "fsk_usb_input_mode": INPUT_MODE,
        "fsk_usb_serial_config": f"{config.get('bitrate') or SERIAL_BITRATE} {SERIAL_FORMAT}",
        "fsk_usb_decode_mode": config.get("decode_mode", "0"),
        "fsk_usb_pdl_port": config.get("port", "1"),
        "fsk_usb_four_level": config.get("four_level", "0"),
        "fsk_usb_pdl_in_use": "0",
        "fsk_usb_last_seen": "",
    }


def main() -> int:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    status = collect_status()
    previous = storage.get_runtime_status()
    previous_ever_seen = str(previous.get("fsk_usb_ever_seen", {}).get("value") or "0") == "1"
    previous_last_seen = str(previous.get("fsk_usb_last_seen", {}).get("value") or "")

    if status.get("fsk_usb_connected") == "1":
        status["fsk_usb_ever_seen"] = "1"
    else:
        status["fsk_usb_ever_seen"] = "1" if previous_ever_seen else "0"
        if previous_ever_seen and previous_last_seen:
            status["fsk_usb_last_seen"] = previous_last_seen

    storage.update_runtime_status(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
