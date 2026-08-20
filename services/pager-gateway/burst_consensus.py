from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from alarm_rules import _quality_noise_reason


_NR_BURST_RE = re.compile(r"\bNR\b.{0,28}?M\+S\b", re.I)
_DISPATCH_KEY_RE = re.compile(
    r"\bNR\s+RI\([^)]{1,16}\)M\+S\b[\s·:;,_-]*(?P<place>[A-Za-zÆØÅæøå][A-Za-zÆØÅæøå-]{3,})",
    re.I,
)


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


_BURST_WAIT_SECONDS = _bounded_float("PAGER_NR_BURST_WAIT_SECONDS", 4.5, 1.0, 8.0)
_BURST_EARLY_FLUSH_SECONDS = _bounded_float("PAGER_NR_BURST_EARLY_FLUSH_SECONDS", 0.35, 0.1, 2.0)
_BURST_RECENT_SECONDS = _bounded_float("PAGER_NR_BURST_RECENT_SECONDS", 10.0, 4.0, 30.0)
_MAX_BURST_CANDIDATES = 12


def _normalized_text(message: str) -> str:
    value = str(message or "").casefold().replace("?", "")
    value = re.sub(r"[^\wÆØÅæøå]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _looks_nr_dispatch(message: str) -> bool:
    return bool(_NR_BURST_RE.search(str(message or "")))


def _dispatch_key(message: str) -> str | None:
    match = _DISPATCH_KEY_RE.search(str(message or ""))
    if not match:
        return None
    return match.group("place").casefold()


def _common_prefix_length(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def same_nr_burst(left: str, right: str) -> bool:
    """Conservatively decide whether two damaged texts belong to one radio burst."""
    if not _looks_nr_dispatch(left) or not _looks_nr_dispatch(right):
        return False

    left_key = _dispatch_key(left)
    right_key = _dispatch_key(right)
    if left_key and left_key == right_key:
        return True

    left_norm = _normalized_text(left)
    right_norm = _normalized_text(right)
    if not left_norm or not right_norm:
        return False

    similarity = SequenceMatcher(None, left_norm, right_norm, autojunk=False).ratio()
    if similarity >= 0.82:
        return True

    shorter, longer = sorted((left_norm, right_norm), key=len)
    # A short intermediate copy can end halfway through the dispatch. Allow it
    # to join a longer copy only when a substantial beginning is identical.
    if len(shorter) <= 44 and len(shorter) >= 24:
        return _common_prefix_length(shorter, longer) >= 20
    return False


def _align_to_anchor(anchor: str, candidate: str) -> tuple[list[str | None], dict[int, str], tuple[int, int] | None]:
    """Align one candidate to anchor coordinates using ordinary edit distance."""
    n = len(anchor)
    m = len(candidate)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j

    for i in range(1, n + 1):
        ai = anchor[i - 1]
        for j in range(1, m + 1):
            substitution = 0 if ai == candidate[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j - 1] + substitution,
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
            )

    ops: list[tuple[str, int, int | None]] = []
    i, j = n, m
    while i or j:
        if i and j and dp[i][j] == dp[i - 1][j - 1] + (0 if anchor[i - 1] == candidate[j - 1] else 1):
            ops.append(("diag", i - 1, j - 1))
            i -= 1
            j -= 1
        elif i and dp[i][j] == dp[i - 1][j] + 1:
            ops.append(("delete", i - 1, None))
            i -= 1
        else:
            ops.append(("insert", i, j - 1))
            j -= 1
    ops.reverse()

    mapped: list[str | None] = [None] * n
    insert_parts: dict[int, list[str]] = {}
    touched: list[int] = []
    for kind, anchor_index, candidate_index in ops:
        if kind == "diag":
            mapped[anchor_index] = candidate[candidate_index]  # type: ignore[index]
            touched.append(anchor_index)
        elif kind == "insert":
            insert_parts.setdefault(anchor_index, []).append(candidate[candidate_index])  # type: ignore[index]

    insertions = {slot: "".join(parts) for slot, parts in insert_parts.items()}
    coverage = (min(touched), max(touched)) if touched else None
    return mapped, insertions, coverage


def consensus_message(messages: list[str]) -> str:
    """Build a character consensus from repeated independently damaged copies.

    This deliberately has no dictionary and no station/address knowledge. A
    character is changed only when the received copies themselves provide a
    majority. Short/truncated copies stop voting after their last aligned char.
    """
    values = [str(value or "").strip() for value in messages if str(value or "").strip()]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]

    max_length = max(len(value) for value in values)
    anchor_candidates = [value for value in values if len(value) >= max(12, int(max_length * 0.72))]
    if not anchor_candidates:
        anchor_candidates = values

    def medoid_score(value: str) -> float:
        return sum(SequenceMatcher(None, value, other, autojunk=False).ratio() for other in values)

    anchor = max(anchor_candidates, key=medoid_score)
    alignments = [_align_to_anchor(anchor, value) for value in values]
    output: list[str] = []

    for slot in range(len(anchor) + 1):
        insertion_votes: dict[str, int] = {}
        insertion_voters = 0
        for _mapped, insertions, coverage in alignments:
            if coverage is None:
                continue
            start, end = coverage
            if start <= min(slot, max(0, len(anchor) - 1)) <= end + 1:
                insertion_voters += 1
                inserted = insertions.get(slot, "")
                if inserted:
                    insertion_votes[inserted] = insertion_votes.get(inserted, 0) + 1
        if insertion_votes:
            inserted, votes = max(insertion_votes.items(), key=lambda item: item[1])
            if votes >= 2 and votes > insertion_voters / 2:
                output.append(inserted)

        if slot == len(anchor):
            break

        votes: dict[str | None, int] = {anchor[slot]: 1}
        for mapped, _insertions, coverage in alignments:
            if coverage is None:
                continue
            start, end = coverage
            if not (start <= slot <= end):
                continue
            char = mapped[slot]
            votes[char] = votes.get(char, 0) + 1

        highest = max(votes.values())
        winners = [char for char, count in votes.items() if count == highest]
        winner: str | None
        if len(winners) == 1:
            winner = winners[0]
        else:
            winner = anchor[slot]
        if winner is not None:
            output.append(winner)

    return "".join(output).strip()


def _parse_moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass
class _Candidate:
    message_id: int
    data: dict[str, Any]


@dataclass
class _Burst:
    burst_id: int
    started_monotonic: float
    candidates: list[_Candidate] = field(default_factory=list)
    timer: threading.Timer | None = None


@dataclass
class _RecentBurst:
    finished_monotonic: float
    representative_id: int
    messages: list[str]


class PocsagBurstConsensus:
    def __init__(self, core: Any, alarm_filter_store: Any) -> None:
        self.core = core
        self.alarm_filter_store = alarm_filter_store
        self._lock = threading.RLock()
        self._next_id = 1
        self._bursts: dict[int, _Burst] = {}
        self._recent: list[_RecentBurst] = []
        self._original_ingest = core.ingest_event
        core.ingest_event = self.ingest

    @staticmethod
    def _is_candidate(event: Any, message: str) -> bool:
        source = str(getattr(event, "source", "") or "").lower()
        baud = getattr(event, "baud", None)
        return source.startswith("pdl") and (baud in {None, 1200}) and _looks_nr_dispatch(message)

    def _store_pending(self, event: Any, message: str) -> tuple[int, dict[str, Any]]:
        data = event.to_dict()
        data["message"] = self.core.public_message(message)
        station, routing_source = self.core.routing.classify(
            data.get("ric"), data.get("station"), data["message"]
        )
        data.update({
            "station": station,
            "routing_source": routing_source,
            "message_fingerprint": self.core.adaptive.exact_signature(data["message"]),
            "relevance_class": "unknown",
            "relevance_score": 0.75,
            "suppressed_reason": "burst-candidate",
            "duplicate_of": None,
            "delivery_eligible": False,
            "decision_reason": "rå POCSAG-kopi gemt; afventer kort burst-samling før levering",
        })
        return self.core.storage.add_message(data), data

    def _mark_duplicate(self, message_id: int, representative_id: int) -> None:
        with self.core.storage.connect() as conn:
            conn.execute(
                """UPDATE messages
                   SET delivery_eligible=0, suppressed_reason='duplicate', duplicate_of=?,
                       decision_reason='POCSAG burst-kopi; rålinjen er bevaret'
                   WHERE id=?""",
                (representative_id, message_id),
            )

    def _prune_recent_locked(self, now: float) -> None:
        cutoff = now - _BURST_RECENT_SECONDS
        self._recent = [item for item in self._recent if item.finished_monotonic >= cutoff][-24:]

    def _recent_match_locked(self, message: str, now: float) -> int | None:
        self._prune_recent_locked(now)
        for item in reversed(self._recent):
            if any(same_nr_burst(message, previous) for previous in item.messages):
                return item.representative_id
        return None

    def _find_active_locked(self, message: str, now: float) -> _Burst | None:
        for burst in self._bursts.values():
            if now - burst.started_monotonic > _BURST_WAIT_SECONDS + 1.0:
                continue
            if any(same_nr_burst(message, candidate.data["message"]) for candidate in burst.candidates):
                return burst
        return None

    def _schedule_locked(self, burst: _Burst, delay: float) -> None:
        if burst.timer is not None:
            burst.timer.cancel()
        timer = threading.Timer(delay, self._flush, args=(burst.burst_id,))
        timer.daemon = True
        burst.timer = timer
        timer.start()

    def ingest(self, event: Any) -> int:
        message = self.core.public_message(str(getattr(event, "message", "") or ""))
        if not self._is_candidate(event, message):
            return self._original_ingest(event)

        message_id, data = self._store_pending(event, message)
        candidate = _Candidate(message_id=message_id, data=data)
        now = time.monotonic()

        with self._lock:
            recent_id = self._recent_match_locked(message, now)
            if recent_id is not None:
                self._mark_duplicate(message_id, recent_id)
                return message_id

            burst = self._find_active_locked(message, now)
            if burst is None:
                burst = _Burst(burst_id=self._next_id, started_monotonic=now)
                self._next_id += 1
                self._bursts[burst.burst_id] = burst
                self._schedule_locked(burst, _BURST_WAIT_SECONDS)

            if len(burst.candidates) < _MAX_BURST_CANDIDATES:
                burst.candidates.append(candidate)
            else:
                self._mark_duplicate(message_id, burst.candidates[0].message_id)
                return message_id

            # Three independent copies are enough for a real majority vote. This
            # keeps the alarm delay around the normal 2-3 s multi-RIC burst rather
            # than always waiting the full fallback window.
            if len(burst.candidates) >= 3:
                self._schedule_locked(burst, _BURST_EARLY_FLUSH_SECONDS)
        return message_id

    def _previous_deliverable_duplicate(self, message: str, received_at: str, exclude_ids: set[int]) -> int | None:
        current = _parse_moment(received_at)
        if current is None:
            return None
        normalized = _normalized_text(message)
        with self.core.storage.connect() as conn:
            rows = conn.execute(
                """SELECT id, received_at, message FROM messages
                   WHERE delivery_eligible=1 ORDER BY id DESC LIMIT 80"""
            ).fetchall()
        for row in rows:
            row_id = int(row["id"])
            if row_id in exclude_ids:
                continue
            previous = _parse_moment(row["received_at"])
            if previous is None:
                continue
            delta = (current - previous).total_seconds()
            if delta < 0 or delta > 90:
                continue
            previous_message = str(row["message"] or "")
            if previous_message == message:
                return row_id
            if not _looks_nr_dispatch(previous_message):
                continue
            previous_normalized = _normalized_text(previous_message)
            if not normalized or not previous_normalized:
                continue
            shorter, longer = sorted((normalized, previous_normalized), key=len)
            containment = len(shorter) >= 28 and shorter in longer
            similarity = SequenceMatcher(None, normalized, previous_normalized, autojunk=False).ratio()
            if containment or similarity >= 0.92:
                return row_id
        return None

    def _decision(self, message: str, representative: dict[str, Any], candidate_ids: set[int]) -> dict[str, Any]:
        quality_reason = _quality_noise_reason(message)
        if quality_reason:
            return {
                "message_fingerprint": self.core.adaptive.exact_signature(message),
                "relevance_class": "noise",
                "relevance_score": 0.0,
                "suppressed_reason": quality_reason,
                "duplicate_of": None,
                "delivery_eligible": False,
                "decision_reason": "pager-kvalitetsfilter efter POCSAG burst-samling",
            }

        matched = self.alarm_filter_store.match(message)
        if matched:
            return {
                "message_fingerprint": self.core.adaptive.exact_signature(message),
                "relevance_class": "filtered",
                "relevance_score": 0.0,
                "suppressed_reason": f"word-filter:{matched}",
                "duplicate_of": None,
                "delivery_eligible": False,
                "decision_reason": f"manuelt ordfilter matchede '{matched}' efter burst-samling",
            }

        automatic_noise = self.core.adaptive.automatic_noise_reason(message)
        if automatic_noise:
            return {
                "message_fingerprint": self.core.adaptive.exact_signature(message),
                "relevance_class": "noise",
                "relevance_score": 0.0,
                "suppressed_reason": automatic_noise,
                "duplicate_of": None,
                "delivery_eligible": False,
                "decision_reason": "automatisk decoder-rens efter POCSAG burst-samling",
            }

        duplicate_of = self._previous_deliverable_duplicate(
            message, str(representative.get("received_at") or ""), candidate_ids
        )
        learned = self.core.adaptive.learned_relevance(message)
        if duplicate_of is not None:
            return {
                "message_fingerprint": self.core.adaptive.exact_signature(message),
                "relevance_class": learned["classification"],
                "relevance_score": float(learned["score"]),
                "suppressed_reason": "duplicate",
                "duplicate_of": duplicate_of,
                "delivery_eligible": False,
                "decision_reason": f"samme NR-dispatch som tidligere melding #{duplicate_of}",
            }

        is_noise = learned["classification"] == "noise"
        return {
            "message_fingerprint": self.core.adaptive.exact_signature(message),
            "relevance_class": learned["classification"],
            "relevance_score": float(learned["score"]),
            "suppressed_reason": "noise" if is_noise else None,
            "duplicate_of": None,
            "delivery_eligible": not is_noise,
            "decision_reason": learned["reason"],
        }

    def _flush(self, burst_id: int) -> None:
        with self._lock:
            burst = self._bursts.pop(burst_id, None)
            if burst is None or not burst.candidates:
                return
            if burst.timer is not None:
                burst.timer.cancel()

        candidates = burst.candidates
        messages = [candidate.data["message"] for candidate in candidates]
        consensus = self.core.public_message(consensus_message(messages))
        representative = candidates[0]
        candidate_ids = {candidate.message_id for candidate in candidates}
        decision = self._decision(consensus, representative.data, candidate_ids)
        station, routing_source = self.core.routing.classify(
            representative.data.get("ric"), representative.data.get("station"), consensus
        )
        copy_count = len(candidates)
        base_reason = str(decision.get("decision_reason") or "")
        decision["decision_reason"] = (
            f"POCSAG burst-samling: {copy_count} rå kopier -> én melding; {base_reason}"
        )[:500]

        with self.core.storage.connect() as conn:
            conn.execute(
                """UPDATE messages
                   SET station=?, message=?, message_fingerprint=?, relevance_class=?, relevance_score=?,
                       suppressed_reason=?, duplicate_of=?, delivery_eligible=?, decision_reason=?
                   WHERE id=?""",
                (
                    station,
                    consensus,
                    decision["message_fingerprint"],
                    decision["relevance_class"],
                    float(decision["relevance_score"]),
                    decision.get("suppressed_reason"),
                    decision.get("duplicate_of"),
                    1 if decision.get("delivery_eligible") else 0,
                    decision["decision_reason"],
                    representative.message_id,
                ),
            )
            for candidate in candidates[1:]:
                conn.execute(
                    """UPDATE messages
                       SET delivery_eligible=0, suppressed_reason='duplicate', duplicate_of=?,
                           decision_reason='POCSAG burst-kopi; rålinjen er bevaret'
                       WHERE id=?""",
                    (representative.message_id, candidate.message_id),
                )

        self.core.adaptive.observe(representative.message_id, consensus)

        with self._lock:
            self._recent.append(
                _RecentBurst(
                    finished_monotonic=time.monotonic(),
                    representative_id=representative.message_id,
                    messages=[consensus, *messages],
                )
            )
            self._prune_recent_locked(time.monotonic())

        if not decision.get("delivery_eligible"):
            return

        outbound = dict(representative.data)
        outbound.update(decision)
        outbound["message"] = consensus
        outbound["station"] = station
        outbound["routing_source"] = routing_source
        try:
            self.core.maybe_notify_pushover(representative.message_id, outbound)
        except Exception as exc:
            self.core.app.logger.warning(
                "Pushover failed for burst message %s: %s", representative.message_id, exc
            )
        threading.Thread(
            target=self.core.send_web_push_for_event,
            args=(representative.message_id, outbound),
            name=f"web-push-burst-{representative.message_id}",
            daemon=True,
        ).start()


def install_burst_consensus(core: Any, alarm_filter_store: Any) -> PocsagBurstConsensus:
    return PocsagBurstConsensus(core, alarm_filter_store)
