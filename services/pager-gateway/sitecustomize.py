"""Runtime compatibility for Python builds without hashlib.scrypt.

Werkzeug 3 uses scrypt as its default password hash. Some older macOS Python
builds (notably system/Command Line Tools Python linked against LibreSSL) do
not expose hashlib.scrypt even though Python 3.9 normally can. The production
Pi/Linux runtime usually has native hashlib.scrypt; this module is a no-op
there.

Python imports sitecustomize automatically during normal startup when this
file is on sys.path (which it is for the pager-gateway application directory).
"""
from __future__ import annotations

import hashlib
from typing import Callable


def _cryptography_scrypt(
    password: bytes,
    *,
    salt: bytes,
    n: int,
    r: int,
    p: int,
    maxmem: int = 0,
    dklen: int = 64,
) -> bytes:
    # maxmem is an OpenSSL/hashlib safety limit. cryptography's Scrypt API
    # does not expose this parameter; Werkzeug supplies a generous value.
    del maxmem
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=dklen, n=n, r=r, p=p)
    return kdf.derive(password)


def install_scrypt_fallback() -> bool:
    """Install hashlib.scrypt only when the runtime does not provide it."""
    if hasattr(hashlib, "scrypt"):
        return False

    # hashlib is a normal module object, so adding the compatible callable is
    # sufficient for Werkzeug's password helpers.
    setattr(hashlib, "scrypt", _cryptography_scrypt)
    return True


FALLBACK_INSTALLED = install_scrypt_fallback()
