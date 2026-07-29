import os
import re
from pathlib import Path

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
MAX_VALUE_LENGTH = 128


def _safe_value(name, default=""):
    value = os.getenv(name, default).strip()
    return value[:MAX_VALUE_LENGTH]


def read_version(path=VERSION_FILE):
    try:
        version = path.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    override = _safe_value("RACHER_OS_VERSION")
    if override:
        version = override
    if not SEMVER_RE.fullmatch(version):
        return "0.0.0+unknown", False
    return version, True


def build_release_payload():
    version, valid = read_version()
    commit = _safe_value("RACHER_OS_COMMIT", "unknown")
    if commit != "unknown" and not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        commit = "unknown"
    channel = _safe_value("RACHER_OS_CHANNEL", "development").lower()
    if channel not in {"development", "preview", "stable"}:
        channel = "development"
    built_at = _safe_value("RACHER_OS_BUILT_AT", "unknown")
    return {
        "product": "Racher OS",
        "version": version,
        "version_valid": valid,
        "channel": channel,
        "commit": commit,
        "built_at": built_at,
        "api_version": 1,
        "read_only": True,
    }
