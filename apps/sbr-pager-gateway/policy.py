from __future__ import annotations

import re

PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{6,14}$")
IGNORE_COMMANDS = {"status", "server status", "serverstatus"}


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s().-]", "", value or "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not phone.startswith("+") and phone.isdigit():
        phone = "+" + phone
    if not PHONE_PATTERN.fullmatch(phone):
        raise ValueError("Ugyldigt telefonnummer")
    return phone


def normalized_command(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def should_ignore_message(body: str) -> bool:
    return normalized_command(body) in IGNORE_COMMANDS
