"""Read sanitized host-monitor state for the Control Center.

The host monitoring scripts run on the Raspberry Pi host where they have the
permissions required for systemd, Docker, hardware and SSH checks.  The web
application only receives the resulting JSON state through a read-only bind
mount.  This deliberately keeps SSH keys and extra host privileges out of the
Control Center container.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def read_monitor_state(path, stale_after_seconds=900, now=None):
    """Return a normalized, bounded view of one monitor state file."""

    state_path = Path(path)
    current_time = int(time.time() if now is None else now)
    stale_after_seconds = max(60, _integer(stale_after_seconds, 900))

    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("state er ikke et JSON-objekt")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "path": str(state_path),
            "stale": True,
            "age_seconds": None,
            "checked_at": None,
            "monitor_status": "unknown",
            "alerted": False,
            "failures": 0,
            "snapshot": {},
            "issues": [],
            "error": str(exc)[:300],
        }

    snapshot = state.get("last_status")
    if not isinstance(snapshot, dict):
        # Backwards compatibility with the local monitor state that predates
        # last_status.  It still gives the dashboard useful health information
        # while the new monitor version is being rolled out.
        snapshot = {}

    checked_at = _integer(
        state.get("checked_at") or snapshot.get("checked_at"),
        0,
    )
    age_seconds = max(0, current_time - checked_at) if checked_at else None
    stale = age_seconds is None or age_seconds > stale_after_seconds

    issues = snapshot.get("issues")
    if not isinstance(issues, list):
        issues = state.get("issues")
    if not isinstance(issues, list):
        issues = []
    issues = [str(item)[:300] for item in issues[:20] if str(item).strip()]

    monitor_status = str(
        snapshot.get("status")
        or state.get("status")
        or ("error" if state.get("alerted") else "unknown")
    ).lower()

    return {
        "available": True,
        "path": str(state_path),
        "stale": stale,
        "age_seconds": age_seconds,
        "checked_at": checked_at or None,
        "monitor_status": monitor_status,
        "alerted": bool(state.get("alerted", monitor_status in {"error", "offline"})),
        "failures": max(0, _integer(state.get("failures"), 0)),
        "snapshot": snapshot,
        "issues": issues,
        "error": None,
    }
