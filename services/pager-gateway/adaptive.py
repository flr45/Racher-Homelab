from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


_WS_RE = re.compile(r"\s+")
_DIGIT_RUN_RE = re.compile(r"\d+")
_DUPLICATE_PUNCT_RE = re.compile(r"[^\wÆØÅæøå]+", re.UNICODE)
_ALPHA_WORD_RE = re.compile(r"[A-Za-zÆØÅæøå]{2,}")
_DECODER_CODE_RE = re.compile(r"^[0-9A-Fa-f*+\-?/\\\[\]{}|ÆØÅæøå\s]{3,120}$")


class AdaptiveFilter:
    """Local, explainable relevance learner.

    Unknown traffic is deliberately treated as real until the admin has supplied
    enough negative feedback. The learner never deletes radio traffic; it only
    annotates messages and decides whether a notification should be delivered.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS adaptive_patterns (
                    kind TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    sample_text TEXT NOT NULL DEFAULT '',
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    relevant_votes INTEGER NOT NULL DEFAULT 0,
                    noise_votes INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT,
                    PRIMARY KEY(kind, signature)
                );

                CREATE TABLE IF NOT EXISTS message_feedback (
                    message_id INTEGER PRIMARY KEY,
                    verdict TEXT NOT NULL CHECK(verdict IN ('relevant', 'noise')),
                    user_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );
                """
            )
            conn.commit()

    @staticmethod
    def normalized_text(text: Any) -> str:
        value = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
        return _WS_RE.sub(" ", value)

    @classmethod
    def exact_signature(cls, text: Any) -> str:
        normalized = cls.normalized_text(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def template_text(cls, text: Any) -> str:
        # Numbers are generalized only for learning suggestions. Exact duplicate
        # detection always uses exact_signature and therefore never conflates
        # different addresses/unit numbers.
        return _DIGIT_RUN_RE.sub("#", cls.normalized_text(text))

    @classmethod
    def template_signature(cls, text: Any) -> str:
        return hashlib.sha256(cls.template_text(text).encode("utf-8")).hexdigest()

    @classmethod
    def duplicate_text(cls, text: Any) -> str:
        """Normalize text for short-window reception duplicate comparison.

        Decoder punctuation and single undecodable characters should not turn the
        same radio burst into multiple phone notifications. Digits are retained so
        different addresses/units remain materially different.
        """
        value = cls.normalized_text(text).replace("?", "")
        value = _DUPLICATE_PUNCT_RE.sub(" ", value)
        return _WS_RE.sub(" ", value).strip()

    @staticmethod
    def automatic_noise_reason(text: Any) -> str | None:
        """Catch unmistakable decoder artifacts before notification delivery.

        These rows are still stored in the message database by the normal ingest
        path; only delivery is suppressed. This deliberately avoids broad keyword
        filtering so an unfamiliar real alarm continues to pass by default.
        """
        value = str(text or "").strip()
        words = _ALPHA_WORD_RE.findall(value)
        if not words and _DECODER_CODE_RE.fullmatch(value):
            return "decoder-code"
        if (
            len(value) <= 48
            and value[:1].islower()
            and len(words) <= 6
            and not re.search(r"\b(?:BRAND|ALARM|ISL|VSBV|ØF|VCT)\b", value, re.I)
        ):
            return "decoder-fragment"
        return None

    def observe(self, message_id: int, text: str) -> None:
        now = self._now()
        exact = self.exact_signature(text)
        template = self.template_signature(text)
        with self._lock, self.connect() as conn:
            for kind, signature, sample in (
                ("exact", exact, self.normalized_text(text)),
                ("template", template, self.template_text(text)),
            ):
                conn.execute(
                    """INSERT INTO adaptive_patterns(kind, signature, sample_text, seen_count, last_seen_at)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(kind, signature) DO UPDATE SET
                           seen_count=adaptive_patterns.seen_count+1,
                           sample_text=excluded.sample_text,
                           last_seen_at=excluded.last_seen_at""",
                    (kind, signature, sample[:500], now),
                )
            conn.commit()

    def _pattern(self, kind: str, signature: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM adaptive_patterns WHERE kind=? AND signature=?",
                (kind, signature),
            ).fetchone()
        return dict(row) if row else None

    def learned_relevance(self, text: str) -> dict[str, Any]:
        exact = self._pattern("exact", self.exact_signature(text)) or {}
        template = self._pattern("template", self.template_signature(text)) or {}

        def stats(row: dict[str, Any]) -> tuple[int, int, int, float]:
            relevant = int(row.get("relevant_votes") or 0)
            noise = int(row.get("noise_votes") or 0)
            total = relevant + noise
            ratio = (noise / total) if total else 0.0
            return relevant, noise, total, ratio

        er, en, et, eratio = stats(exact)
        tr, tn, tt, tratio = stats(template)

        if et >= 3 and en >= 3 and eratio >= 0.95:
            return {
                "classification": "noise",
                "score": max(0.01, 1.0 - eratio),
                "reason": f"lært støjmønster · {en}/{et} feedback",
                "source": "exact",
            }
        if tt >= 10 and tn >= 10 and tr == 0 and tratio >= 0.98:
            return {
                "classification": "noise",
                "score": max(0.01, 1.0 - tratio),
                "reason": f"lært støjskabelon · {tn}/{tt} feedback",
                "source": "template",
            }

        if et and er > en:
            score = (er + 1) / (et + 2)
            return {
                "classification": "relevant",
                "score": max(0.55, score),
                "reason": f"positiv feedback · {er}/{et}",
                "source": "exact",
            }

        return {
            "classification": "unknown",
            "score": 0.75,
            "reason": "ukendt mønster · behandles som rigtig melding",
            "source": "default",
        }

    @staticmethod
    def _parse_moment(value: Any) -> datetime | None:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _same_signal(candidate: sqlite3.Row, ric: str | None, function: str | None) -> bool:
        # Older callers did not pass RIC/function into evaluate(). In that case
        # fall back to text similarity instead of silently disabling fuzzy dedup.
        if ric is None and function is None:
            return True

        old_ric = str(candidate["ric"] or "").strip()
        new_ric = str(ric or "").strip()
        if bool(old_ric) != bool(new_ric):
            return False
        if old_ric and old_ric != new_ric:
            return False

        old_function = str(candidate["function"] or "").strip()
        new_function = str(function or "").strip()
        if old_function and new_function and old_function != new_function:
            return False
        return True

    def immediate_duplicate(
        self,
        text: str,
        received_at: str,
        window_seconds: int = 30,
        ric: str | None = None,
        function: str | None = None,
    ) -> int | None:
        """Find exact or near-identical recent reception variants.

        Exact duplicates are searched across recent rows rather than only the
        immediately preceding row, so an interleaved decoder artifact no longer
        defeats suppression. Near-duplicates use a tighter eight-second window.
        """
        current = self._parse_moment(received_at)
        if current is None:
            return None

        window = max(1, min(int(window_seconds), 300))
        fingerprint = self.exact_signature(text)
        normalized = self.duplicate_text(text)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT id, received_at, message_fingerprint, message, ric, function
                   FROM messages ORDER BY id DESC LIMIT 80"""
            ).fetchall()

        for row in rows:
            previous = self._parse_moment(row["received_at"])
            if previous is None:
                continue
            delta = (current - previous).total_seconds()
            if delta < 0 or delta > window:
                continue
            if str(row["message_fingerprint"] or "") == fingerprint:
                return int(row["id"])

        fuzzy_window = min(window, 8)
        if len(normalized) < 12:
            return None
        for row in rows:
            if not self._same_signal(row, ric, function):
                continue
            previous = self._parse_moment(row["received_at"])
            if previous is None:
                continue
            delta = (current - previous).total_seconds()
            if delta < 0 or delta > fuzzy_window:
                continue

            previous_text = self.duplicate_text(row["message"])
            if len(previous_text) < 12:
                continue
            shorter, longer = sorted((normalized, previous_text), key=len)
            containment = len(shorter) >= 14 and shorter in longer
            ratio = SequenceMatcher(None, normalized, previous_text, autojunk=False).ratio()
            if containment or ratio >= 0.92:
                return int(row["id"])
        return None

    def evaluate(
        self,
        text: str,
        received_at: str,
        duplicate_window_seconds: int = 30,
        ric: str | None = None,
        function: str | None = None,
    ) -> dict[str, Any]:
        automatic_noise = self.automatic_noise_reason(text)
        if automatic_noise:
            return {
                "message_fingerprint": self.exact_signature(text),
                "relevance_class": "noise",
                "relevance_score": 0.0,
                "suppressed_reason": automatic_noise,
                "duplicate_of": None,
                "delivery_eligible": False,
                "decision_reason": "automatisk decoder-rens: rålinjen gemmes, notifikation undertrykkes",
            }

        learned = self.learned_relevance(text)
        duplicate_of = self.immediate_duplicate(
            text,
            received_at,
            duplicate_window_seconds,
            ric=ric,
            function=function,
        )
        if duplicate_of is not None:
            return {
                "message_fingerprint": self.exact_signature(text),
                "relevance_class": learned["classification"],
                "relevance_score": float(learned["score"]),
                "suppressed_reason": "duplicate",
                "duplicate_of": duplicate_of,
                "delivery_eligible": False,
                "decision_reason": f"gentagelse eller modtagevariant af melding #{duplicate_of}",
            }
        is_noise = learned["classification"] == "noise"
        return {
            "message_fingerprint": self.exact_signature(text),
            "relevance_class": learned["classification"],
            "relevance_score": float(learned["score"]),
            "suppressed_reason": "noise" if is_noise else None,
            "duplicate_of": None,
            "delivery_eligible": not is_noise,
            "decision_reason": learned["reason"],
        }

    def record_feedback(self, message_id: int, verdict: str, user_id: int | None) -> dict[str, Any]:
        verdict = str(verdict or "").strip().lower()
        if verdict not in {"relevant", "noise"}:
            raise ValueError("feedback skal være relevant eller noise")
        with self._lock, self.connect() as conn:
            message = conn.execute("SELECT id, message FROM messages WHERE id=?", (message_id,)).fetchone()
            if not message:
                raise ValueError("meldingen findes ikke")
            previous = conn.execute(
                "SELECT verdict FROM message_feedback WHERE message_id=?", (message_id,)
            ).fetchone()
            text = str(message["message"])
            exact = self.exact_signature(text)
            template = self.template_signature(text)

            now = self._now()
            for kind, signature, sample in (
                ("exact", exact, self.normalized_text(text)),
                ("template", template, self.template_text(text)),
            ):
                conn.execute(
                    """INSERT OR IGNORE INTO adaptive_patterns(
                           kind, signature, sample_text, seen_count, relevant_votes,
                           noise_votes, last_seen_at
                       ) VALUES (?, ?, ?, 0, 0, 0, ?)""",
                    (kind, signature, sample[:500], now),
                )

            if previous:
                old = str(previous["verdict"])
                old_col = "relevant_votes" if old == "relevant" else "noise_votes"
                for kind, signature in (("exact", exact), ("template", template)):
                    conn.execute(
                        f"UPDATE adaptive_patterns SET {old_col}=MAX(0, {old_col}-1) WHERE kind=? AND signature=?",
                        (kind, signature),
                    )

            conn.execute(
                """INSERT INTO message_feedback(message_id, verdict, user_id, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                       verdict=excluded.verdict, user_id=excluded.user_id, created_at=excluded.created_at""",
                (message_id, verdict, user_id, now),
            )
            new_col = "relevant_votes" if verdict == "relevant" else "noise_votes"
            for kind, signature in (("exact", exact), ("template", template)):
                conn.execute(
                    f"UPDATE adaptive_patterns SET {new_col}={new_col}+1 WHERE kind=? AND signature=?",
                    (kind, signature),
                )
            conn.commit()
        return self.learned_relevance(text)

    def review_queue(self, limit: int = 40) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT m.id, m.received_at, m.ric, m.station, m.message,
                          m.relevance_class, m.relevance_score, m.suppressed_reason,
                          m.duplicate_of, mf.verdict AS feedback
                   FROM messages m
                   LEFT JOIN message_feedback mf ON mf.message_id=m.id
                   ORDER BY m.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            messages = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            feedback = conn.execute("SELECT COUNT(*) AS c FROM message_feedback").fetchone()["c"]
            noise = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE suppressed_reason='noise' OR suppressed_reason LIKE 'decoder-%'"
            ).fetchone()["c"]
            duplicates = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE suppressed_reason='duplicate'").fetchone()["c"]
            learned = conn.execute(
                """SELECT COUNT(*) AS c FROM adaptive_patterns
                   WHERE (kind='exact' AND noise_votes>=3) OR (kind='template' AND noise_votes>=10)"""
            ).fetchone()["c"]
        return {
            "messages": int(messages),
            "feedback": int(feedback),
            "noise_suppressed": int(noise),
            "duplicates_suppressed": int(duplicates),
            "learned_noise_patterns": int(learned),
        }
