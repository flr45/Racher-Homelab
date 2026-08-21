from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_FILTERS: tuple[tuple[str, str], ...] = (
    ("0174760", "Fast diagnostik / decoder-test"),
)

# A blocked/noisy RIC can occasionally be the result of address-bit corruption
# during a genuine dispatch. If the alpha payload still contains strong operational
# structure, fail open and let the normal quality/dedupe pipeline decide. This is
# intentionally narrow: ordinary numeric diagnostic traffic never matches it.
_OPERATIONAL_RESCUE_RE = re.compile(
    r"(?:\b(?:ISL|BRAND(?:ALARM)?|ALARM|VSBV|ØF|VCT)\b|M\+[SV])",
    re.I,
)
_ALPHA_RE = re.compile(r"[A-Za-zÆØÅæøå]")


def _looks_operational_alarm(message: Any) -> bool:
    value = str(message or "").strip()
    if len(value) < 8 or not _OPERATIONAL_RESCUE_RE.search(value):
        return False
    # Require enough alphabetic payload that a chance three-letter corruption on
    # the diagnostic RIC cannot bypass the deny-list by itself.
    return len(_ALPHA_RE.findall(value)) >= 6


class RicNoiseFilter:
    """Persistent RIC deny-list for known diagnostic/noise capcodes.

    Matching messages remain untouched in the raw message history. The same
    deny-list is used by live ingestion and the adaptive learning queue: a
    blocked RIC is retained for diagnostics, but is not delivered as an alarm,
    Web Push/Pushover notification or learning-review item unless a damaged
    payload still carries strong operational alarm structure.
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
        return conn

    @staticmethod
    def normalize_ric(value: Any) -> str:
        ric = str(value or "").strip()
        if not ric.isdigit() or not 4 <= len(ric) <= 10:
            raise ValueError("RIC/capcode skal være 4-10 cifre")
        return ric

    def _initialize(self) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS adaptive_ric_filters (
                       ric TEXT PRIMARY KEY,
                       label TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL,
                       created_by INTEGER
                   )"""
            )
            now = self._now()
            conn.executemany(
                """INSERT OR IGNORE INTO adaptive_ric_filters(ric, label, created_at, created_by)
                   VALUES (?, ?, ?, NULL)""",
                [(ric, label, now) for ric, label in DEFAULT_FILTERS],
            )
            conn.commit()

    def list_filters(self) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT ric, label, created_at, created_by FROM adaptive_ric_filters ORDER BY ric"
            ).fetchall()
        return [dict(row) for row in rows]

    def filtered_rics(self) -> set[str]:
        return {str(row["ric"]) for row in self.list_filters()}

    def contains(self, ric: Any) -> bool:
        value = str(ric or "").strip()
        if not value:
            return False
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM adaptive_ric_filters WHERE ric=?", (value,)
            ).fetchone()
        return row is not None

    def add(self, ric: Any, label: Any = "", user_id: int | None = None) -> dict[str, Any]:
        clean_ric = self.normalize_ric(ric)
        clean_label = " ".join(str(label or "").strip().split())[:120]
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO adaptive_ric_filters(ric, label, created_at, created_by)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ric) DO UPDATE SET
                       label=excluded.label,
                       created_by=COALESCE(excluded.created_by, adaptive_ric_filters.created_by)""",
                (clean_ric, clean_label, self._now(), user_id),
            )
            row = conn.execute(
                "SELECT ric, label, created_at, created_by FROM adaptive_ric_filters WHERE ric=?",
                (clean_ric,),
            ).fetchone()
            conn.commit()
        return dict(row)

    def remove(self, ric: Any) -> bool:
        clean_ric = self.normalize_ric(ric)
        with self._lock, self.connect() as conn:
            cur = conn.execute("DELETE FROM adaptive_ric_filters WHERE ric=?", (clean_ric,))
            conn.commit()
        return cur.rowcount > 0

    def filter_review_rows(self, rows: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
        blocked = self.filtered_rics()
        result = [
            row
            for row in rows
            if str(row.get("ric") or "").strip() not in blocked
            or _looks_operational_alarm(row.get("message"))
        ]
        return result[: max(1, int(limit))]


def install_live_ric_filter(core: Any, ric_noise: RicNoiseFilter) -> None:
    """Wrap ``core.ingest_event`` so blocked RICs are retained but normally not delivered.

    The wrapper intentionally fails open if the SQLite lookup itself fails: a
    filter/database problem must not make a real emergency page disappear. It
    also fails open for a blocked RIC when the payload still contains strong
    operational alarm structure; address corruption during a real dispatch must
    not let a diagnostic deny-list hide the whole incident.
    """
    if getattr(core, "_ric_noise_filter_installed", False):
        return

    original_ingest = core.ingest_event

    def filtered_ingest(event):
        try:
            if ric_noise.contains(getattr(event, "ric", None)):
                existing_reason = str(getattr(event, "decoder_noise_reason", None) or "").strip()
                message = getattr(event, "message", "")
                if existing_reason:
                    # A stronger upstream decoder-quality decision already exists.
                    # Never replace it merely because the RIC is also blocklisted.
                    pass
                elif _looks_operational_alarm(message):
                    try:
                        core.app.logger.warning(
                            "RIC blocklist bypassed for operational alarm fragment: ric=%s",
                            getattr(event, "ric", None),
                        )
                    except Exception:
                        pass
                else:
                    event.decoder_noise_reason = "ric-filter"
        except Exception as exc:  # fail open for alarm delivery
            try:
                core.app.logger.warning("RIC blocklist lookup failed: %s", exc)
            except Exception:
                pass
        return original_ingest(event)

    core.ingest_event = filtered_ingest
    core._ric_noise_filter_installed = True
    core.ric_noise_filter = ric_noise
