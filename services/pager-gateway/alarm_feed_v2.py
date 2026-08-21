from __future__ import annotations

import re
from typing import Any


_ALARM_HINT_RE = re.compile(r"(?:\b(?:BRAND(?:ALARM)?|ALARM|ISL|VSBV|ØF|VCT)\b|M\+S)", re.I)
_POSTAL_LOCALITY_RE = re.compile(r"\b\d{4}\s+[A-Za-zÆØÅæøå][A-Za-zÆØÅæøå-]{2,}\b")
_SUSPICIOUS_RE = re.compile(r"[?%`�\\]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MIN_PROMOTION_GAIN = 8.0


def display_quality_score(message: Any) -> float:
    """Score how useful one already-decoded copy is for the public alarm feed.

    This is deliberately not a relevance classifier. It is only used after the
    normal duplicate logic has already decided that two rows represent the same
    dispatch. A later copy therefore cannot create or merge an incident here; it
    can only replace the display text of that incident when it is materially
    cleaner/more complete.
    """
    value = " ".join(str(message or "").split())
    if not value:
        return -1000.0

    score = min(len(value), 240) * 0.20
    words = re.findall(r"[A-Za-zÆØÅæøå]{2,}", value)
    score += min(len(words), 20) * 0.55

    if _ALARM_HINT_RE.search(value):
        score += 14.0
    if _POSTAL_LOCALITY_RE.search(value):
        score += 16.0

    suspicious = len(_SUSPICIOUS_RE.findall(value))
    score -= suspicious * 7.0

    compact = "".join(char for char in value if not char.isspace())
    if compact:
        ordinary = sum(
            1
            for char in compact
            if char.isalnum() or char in "ÆØÅæøå.,:;·()+-/_'\""
        )
        odd_ratio = max(0.0, 1.0 - ordinary / len(compact))
        score -= odd_ratio * 30.0

    return score


def _duplicate_root(conn: Any, message_id: int) -> tuple[int, dict[str, Any]] | None:
    current_id = int(message_id)
    seen: set[int] = set()
    while current_id not in seen and len(seen) < 128:
        seen.add(current_id)
        row = conn.execute("SELECT * FROM messages WHERE id=?", (current_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        parent = data.get("duplicate_of")
        if parent is None:
            return current_id, data
        current_id = int(parent)
    return None


def promote_duplicate_display(core: Any, message_id: int) -> bool:
    """Promote a cleaner duplicate copy to the root row's public display text.

    raw_line, received_at, RIC metadata and the duplicate row itself remain
    untouched. Only an already delivery-eligible root may be upgraded.
    """
    with core.storage.connect() as conn:
        duplicate = conn.execute(
            "SELECT * FROM messages WHERE id=?", (int(message_id),)
        ).fetchone()
        if duplicate is None or duplicate["duplicate_of"] is None:
            return False

        root = _duplicate_root(conn, int(message_id))
        if root is None:
            return False
        root_id, root_row = root
        if not bool(root_row.get("delivery_eligible")):
            return False

        candidate_message = str(duplicate["message"] or "").strip()
        current_message = str(root_row.get("message") or "").strip()
        if not candidate_message or candidate_message == current_message:
            return False

        gain = display_quality_score(candidate_message) - display_quality_score(current_message)
        if gain < _MIN_PROMOTION_GAIN:
            return False

        station, _routing_source = core.routing.classify(
            root_row.get("ric"), root_row.get("station"), candidate_message
        )
        old_reason = str(root_row.get("decision_reason") or "").strip()
        promotion_reason = f"Alarmfeed v2: visning opgraderet fra bedre dublet #{int(message_id)}"
        reason = f"{old_reason}; {promotion_reason}" if old_reason else promotion_reason

        conn.execute(
            """UPDATE messages
               SET station=?, message=?, message_fingerprint=?, decision_reason=?
               WHERE id=? AND delivery_eligible=1""",
            (
                station,
                candidate_message,
                core.adaptive.exact_signature(candidate_message),
                reason[:500],
                root_id,
            ),
        )
        return True


def install_alarm_feed_v2(core: Any):
    """Wrap the final ingest path with best-copy promotion for duplicate rows."""
    original_ingest = core.ingest_event

    def ingest_with_best_copy(event: Any) -> int:
        message_id = original_ingest(event)
        try:
            promote_duplicate_display(core, message_id)
        except Exception as exc:  # noqa: BLE001
            core.app.logger.warning(
                "Alarmfeed v2 best-copy promotion failed for message %s: %s",
                message_id,
                exc,
            )
        return message_id

    core.ingest_event = ingest_with_best_copy
    return ingest_with_best_copy
