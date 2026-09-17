from __future__ import annotations

import hashlib
import hmac
import os

from storage import get_setting, set_setting

ITERATIONS = 310_000


def pin_is_configured() -> bool:
    return bool(get_setting("admin_pin_salt", "") and get_setting("admin_pin_hash", ""))


def _validate_pin(pin: str) -> str:
    value = pin.strip()
    if len(value) != 6 or not value.isdigit():
        raise ValueError("Admin-PIN skal være præcis 6 cifre.")
    return value


def set_admin_pin(pin: str) -> None:
    value = _validate_pin(pin)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        ITERATIONS,
    )
    set_setting("admin_pin_salt", salt.hex())
    set_setting("admin_pin_hash", digest.hex())
    set_setting("admin_pin_iterations", str(ITERATIONS))


def verify_admin_pin(pin: str) -> bool:
    try:
        value = _validate_pin(pin)
    except ValueError:
        return False
    salt_hex = get_setting("admin_pin_salt", "") or ""
    hash_hex = get_setting("admin_pin_hash", "") or ""
    if not salt_hex or not hash_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        iterations = int(get_setting("admin_pin_iterations", str(ITERATIONS)) or ITERATIONS)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        max(100_000, iterations),
    )
    return hmac.compare_digest(actual, expected)
