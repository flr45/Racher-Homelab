from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_WS_RE = re.compile(r"\s+")
_DIGIT_RUN_RE = re.compile(r"\d+")


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

        # Exact patterns can suppress after three unanimous/almost-unanimous noise
        # votes. Generalized templates require substantially more evidence and no
        # contradicting relevant vote.
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

    def immediate_duplicate(self, text: str, received_at: str, window_seconds: int = 30) -> int | None:
        fingerprint = self.exact_signature(text)
        with self._lock, self.connect() as conn:
            row = conn.execute(
                """SELECT id, received_at, message_fingerprint
                   FROM messages ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        if not row or str(row["message_fingerprint"] or "") != fingerprint:
            return None
        try:
            current = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            previous = datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00"))
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            delta = (current.astimezone(timezone.utc) - previous.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
        if 0 <= delta <= max(1, min(int(window_seconds), 300)):
            return int(row["id"])
        return None

    def evaluate(self, text: str, received_at: str, duplicate_window_seconds: int = 30) -> dict[str, Any]:
        learned = self.learned_relevance(text)
        duplicate_of = self.immediate_duplicate(text, received_at, duplicate_window_seconds)
        if duplicate_of is not None:
            return {
                "message_fingerprint": self.exact_signature(text),
                "relevance_class": learned["classification"],
                "relevance_score": float(learned["score"]),
                "suppressed_reason": "duplicate",
                "duplicate_of": duplicate_of,
                "delivery_eligible": False,
                "decision_reason": f"identisk med melding #{duplicate_of} lige før",
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
                (message_id, verdict, user_id, self._now()),
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
            noise = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE suppressed_reason='noise'").fetchone()["c"]
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
