from __future__ import annotations

import csv
import io
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway import parse_pdl_line, public_message


class TrainingStore:
    """Isolated replay/training workspace.

    Replay rows never enter the live ``messages`` table and never call the live
    ingestion/notification path. Admin explicitly applies selected learning after
    reviewing the report.
    """

    MAX_REPLAY_LINES = 20000
    MAX_IMPORT_LINES = 5000

    def __init__(self, db_path: str, routing: Any, adaptive: Any) -> None:
        self.db_path = str(Path(db_path))
        self.routing = routing
        self.adaptive = adaptive
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
                CREATE TABLE IF NOT EXISTS training_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    total_lines INTEGER NOT NULL DEFAULT 0,
                    parsed_count INTEGER NOT NULL DEFAULT 0,
                    real_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    noise_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    unclassified_count INTEGER NOT NULL DEFAULT 0,
                    station_candidate_count INTEGER NOT NULL DEFAULT 0,
                    ric_candidate_count INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS training_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    line_no INTEGER NOT NULL,
                    raw_line TEXT NOT NULL,
                    message TEXT NOT NULL,
                    ric TEXT,
                    station TEXT,
                    routing_source TEXT NOT NULL DEFAULT 'unknown',
                    relevance_class TEXT NOT NULL DEFAULT 'unknown',
                    relevance_score REAL NOT NULL DEFAULT 0.75,
                    suppressed_reason TEXT,
                    duplicate_of_event_id INTEGER,
                    decision_reason TEXT NOT NULL DEFAULT '',
                    feedback TEXT CHECK(feedback IN ('relevant', 'noise') OR feedback IS NULL),
                    FOREIGN KEY(run_id) REFERENCES training_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(duplicate_of_event_id) REFERENCES training_events(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_training_events_run ON training_events(run_id, line_no);

                CREATE TABLE IF NOT EXISTS training_station_candidates (
                    run_id INTEGER NOT NULL,
                    station_name TEXT NOT NULL COLLATE NOCASE,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    sample_message TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending', 'approved', 'rejected')),
                    PRIMARY KEY(run_id, station_name),
                    FOREIGN KEY(run_id) REFERENCES training_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS training_ric_candidates (
                    run_id INTEGER NOT NULL,
                    ric TEXT NOT NULL,
                    station_name TEXT NOT NULL COLLATE NOCASE,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    sample_message TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending', 'approved', 'rejected')),
                    PRIMARY KEY(run_id, ric, station_name),
                    FOREIGN KEY(run_id) REFERENCES training_runs(id) ON DELETE CASCADE
                );
                """
            )
            conn.commit()

    def _preview_classification(self, ric: str, fallback_station: str, message: str) -> dict[str, Any]:
        clean_ric = str(ric or "").strip()
        if clean_ric:
            with self.routing.connect() as conn:
                row = conn.execute(
                    "SELECT station_key FROM ric_codes WHERE ric=? AND active=1", (clean_ric,)
                ).fetchone()
            if row:
                name = self.routing.station_name(row["station_key"])
                if name:
                    return {"station": name, "source": "ric", "candidate": None}

        if fallback_station:
            key = self.routing.station_key(fallback_station)
            if key:
                return {"station": self.routing.station_name(key), "source": "marker", "candidate": None}

        candidate = self.routing._extract_explicit_station_name(message)  # shared live discovery parser
        if candidate:
            existing_key = self.routing.station_key(candidate)
            if existing_key:
                return {
                    "station": self.routing.station_name(existing_key),
                    "source": "explicit-existing",
                    "candidate": candidate,
                }
            return {"station": None, "source": "training-candidate", "candidate": candidate}
        return {"station": None, "source": "unknown", "candidate": None}

    def create_replay(self, name: Any, raw_text: Any, created_by: int = None) -> dict[str, Any]:
        run_name = " ".join(str(name or "Træningskørsel").strip().split())[:120] or "Træningskørsel"
        lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
        if not lines:
            raise ValueError("Indsæt mindst én melding/loglinje")
        if len(lines) > self.MAX_REPLAY_LINES:
            raise ValueError("For mange linjer i én replay-kørsel")

        now = self._now()
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO training_runs(name, created_by, created_at, total_lines) VALUES (?, ?, ?, ?)",
                (run_name, created_by, now, len(lines)),
            )
            run_id = int(cur.lastrowid)
            conn.commit()

        previous_fingerprint = None
        previous_event_id = None
        parsed_count = 0
        real_count = 0
        duplicate_count = 0
        noise_count = 0
        unknown_count = 0
        unclassified_count = 0
        station_counts: Counter[str] = Counter()
        station_samples: dict[str, str] = {}
        ric_counts: Counter[tuple[str, str]] = Counter()
        ric_samples: dict[tuple[str, str], str] = {}

        for line_no, raw_line in enumerate(lines, start=1):
            event = parse_pdl_line(raw_line, source="training-replay")
            if not event:
                continue
            message = public_message(event.message)
            if not message:
                continue
            parsed_count += 1
            learned = self.adaptive.learned_relevance(message)
            fingerprint = self.adaptive.exact_signature(message)
            duplicate_of = previous_event_id if previous_fingerprint == fingerprint else None
            suppressed_reason = "duplicate" if duplicate_of else ("noise" if learned["classification"] == "noise" else None)
            if suppressed_reason == "duplicate":
                duplicate_count += 1
            elif suppressed_reason == "noise":
                noise_count += 1
            else:
                real_count += 1
            if learned["classification"] == "unknown":
                unknown_count += 1

            preview = self._preview_classification(event.ric or "", event.station or "", message)
            if not preview["station"]:
                unclassified_count += 1
            candidate = preview.get("candidate")
            if candidate and not self.routing.station_key(candidate):
                station_counts[candidate] += 1
                station_samples.setdefault(candidate, message)

            ric = str(event.ric or "").strip()
            ric_station = preview.get("station") or candidate
            if ric and ric_station:
                key = (ric, str(ric_station))
                ric_counts[key] += 1
                ric_samples.setdefault(key, message)

            with self._lock, self.connect() as conn:
                cur = conn.execute(
                    """INSERT INTO training_events(
                           run_id, line_no, raw_line, message, ric, station, routing_source,
                           relevance_class, relevance_score, suppressed_reason,
                           duplicate_of_event_id, decision_reason
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, line_no, raw_line, message, ric or None, preview.get("station"),
                        preview.get("source") or "unknown", learned["classification"],
                        float(learned["score"]), suppressed_reason, duplicate_of,
                        (
                            "identisk med forrige replay-melding"
                            if duplicate_of else learned["reason"]
                        ),
                    ),
                )
                event_id = int(cur.lastrowid)
                conn.commit()
            previous_fingerprint = fingerprint
            previous_event_id = event_id

        with self._lock, self.connect() as conn:
            for station_name, count in station_counts.items():
                conn.execute(
                    """INSERT INTO training_station_candidates(
                           run_id, station_name, seen_count, sample_message
                       ) VALUES (?, ?, ?, ?)""",
                    (run_id, station_name, count, station_samples[station_name][:500]),
                )
            for (ric, station_name), count in ric_counts.items():
                existing = conn.execute("SELECT 1 FROM ric_codes WHERE ric=?", (ric,)).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO training_ric_candidates(
                               run_id, ric, station_name, seen_count, sample_message
                           ) VALUES (?, ?, ?, ?, ?)""",
                        (run_id, ric, station_name, count, ric_samples[(ric, station_name)][:500]),
                    )
            station_candidate_count = conn.execute(
                "SELECT COUNT(*) AS c FROM training_station_candidates WHERE run_id=?", (run_id,)
            ).fetchone()["c"]
            ric_candidate_count = conn.execute(
                "SELECT COUNT(*) AS c FROM training_ric_candidates WHERE run_id=?", (run_id,)
            ).fetchone()["c"]
            conn.execute(
                """UPDATE training_runs SET
                       parsed_count=?, real_count=?, duplicate_count=?, noise_count=?, unknown_count=?,
                       unclassified_count=?, station_candidate_count=?, ric_candidate_count=?
                   WHERE id=?""",
                (
                    parsed_count, real_count, duplicate_count, noise_count, unknown_count,
                    unclassified_count, int(station_candidate_count), int(ric_candidate_count), run_id,
                ),
            )
            conn.commit()
        return self.get_run(run_id, include_events=True)

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM training_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: int, include_events: bool = True) -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM training_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Træningskørslen findes ikke")
            result = dict(row)
            result["station_candidates"] = [
                dict(item) for item in conn.execute(
                    "SELECT * FROM training_station_candidates WHERE run_id=? ORDER BY seen_count DESC, station_name",
                    (run_id,),
                ).fetchall()
            ]
            result["ric_candidates"] = [
                dict(item) for item in conn.execute(
                    "SELECT * FROM training_ric_candidates WHERE run_id=? ORDER BY seen_count DESC, ric",
                    (run_id,),
                ).fetchall()
            ]
            if include_events:
                result["events"] = [
                    dict(item) for item in conn.execute(
                        """SELECT * FROM training_events
                           WHERE run_id=? ORDER BY line_no LIMIT 1000""",
                        (run_id,),
                    ).fetchall()
                ]
        return result

    def set_event_feedback(self, event_id: int, feedback: Any) -> dict[str, Any]:
        value = str(feedback or "").strip().lower()
        if value not in {"", "relevant", "noise"}:
            raise ValueError("Feedback skal være relevant, noise eller tom")
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM training_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise ValueError("Træningsmeldingen findes ikke")
            conn.execute(
                "UPDATE training_events SET feedback=? WHERE id=?",
                (value or None, event_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM training_events WHERE id=?", (event_id,)).fetchone()
        return dict(updated)

    def set_candidate_decisions(self, run_id: int, stations: Any, rics: Any) -> dict[str, Any]:
        valid = {"pending", "approved", "rejected"}
        with self._lock, self.connect() as conn:
            if not conn.execute("SELECT 1 FROM training_runs WHERE id=?", (run_id,)).fetchone():
                raise ValueError("Træningskørslen findes ikke")
            for item in stations if isinstance(stations, list) else []:
                name = str((item or {}).get("station_name") or "").strip()
                decision = str((item or {}).get("decision") or "pending").strip().lower()
                if name and decision in valid:
                    conn.execute(
                        "UPDATE training_station_candidates SET decision=? WHERE run_id=? AND station_name=? COLLATE NOCASE",
                        (decision, run_id, name),
                    )
            for item in rics if isinstance(rics, list) else []:
                ric = str((item or {}).get("ric") or "").strip()
                station_name = str((item or {}).get("station_name") or "").strip()
                decision = str((item or {}).get("decision") or "pending").strip().lower()
                if ric and station_name and decision in valid:
                    conn.execute(
                        """UPDATE training_ric_candidates SET decision=?
                           WHERE run_id=? AND ric=? AND station_name=? COLLATE NOCASE""",
                        (decision, run_id, ric, station_name),
                    )
            conn.commit()
        return self.get_run(run_id, include_events=False)

    def _apply_text_feedback(self, text: str, verdict: str) -> None:
        if verdict not in {"relevant", "noise"}:
            return
        now = self._now()
        exact = self.adaptive.exact_signature(text)
        template = self.adaptive.template_signature(text)
        column = "relevant_votes" if verdict == "relevant" else "noise_votes"
        with self.adaptive.connect() as conn:
            for kind, signature, sample in (
                ("exact", exact, self.adaptive.normalized_text(text)),
                ("template", template, self.adaptive.template_text(text)),
            ):
                conn.execute(
                    """INSERT INTO adaptive_patterns(
                           kind, signature, sample_text, seen_count, relevant_votes, noise_votes, last_seen_at
                       ) VALUES (?, ?, ?, 1, ?, ?, ?)
                       ON CONFLICT(kind, signature) DO UPDATE SET
                           seen_count=adaptive_patterns.seen_count+1,
                           sample_text=excluded.sample_text,
                           last_seen_at=excluded.last_seen_at""",
                    (
                        kind, signature, sample[:500],
                        1 if column == "relevant_votes" else 0,
                        1 if column == "noise_votes" else 0,
                        now,
                    ),
                )
                conn.execute(
                    f"UPDATE adaptive_patterns SET {column}={column}+1 WHERE kind=? AND signature=?",
                    (kind, signature),
                )
                conn.execute(
                    f"""UPDATE adaptive_patterns SET {column}={column}-1
                        WHERE kind=? AND signature=? AND {column}>0 AND seen_count=1""",
                    (kind, signature),
                )
            conn.commit()

    def apply_run(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id, include_events=False)
        if run.get("applied_at"):
            raise ValueError("Træningskørslen er allerede anvendt")

        stations_created = 0
        rics_created = 0
        feedback_applied = 0

        for item in run["station_candidates"]:
            if item["decision"] != "approved":
                continue
            if not self.routing.station_key(item["station_name"]):
                self.routing.create_station(
                    item["station_name"], auto_created=False,
                    confidence=min(0.99, 0.70 + min(int(item["seen_count"]), 10) * 0.03),
                    source="training-approved",
                )
                stations_created += 1

        for item in run["ric_candidates"]:
            if item["decision"] != "approved":
                continue
            if not self.routing.station_key(item["station_name"]):
                continue
            with self.routing.connect() as conn:
                existing = conn.execute("SELECT 1 FROM ric_codes WHERE ric=?", (item["ric"],)).fetchone()
            if existing:
                continue
            self.routing.create_ric_code(
                item["ric"], self.routing.station_key(item["station_name"]),
                "Godkendt fra trænings-replay", True, None,
            )
            rics_created += 1

        with self._lock, self.connect() as conn:
            feedback_rows = conn.execute(
                """SELECT message, feedback FROM training_events
                   WHERE run_id=? AND feedback IN ('relevant', 'noise') ORDER BY line_no""",
                (run_id,),
            ).fetchall()
        for event in feedback_rows:
            self._apply_text_feedback(event["message"], event["feedback"])
            feedback_applied += 1

        applied_at = self._now()
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE training_runs SET applied_at=? WHERE id=?", (applied_at, run_id))
            conn.commit()
        return {
            "run_id": run_id,
            "stations_created": stations_created,
            "rics_created": rics_created,
            "feedback_applied": feedback_applied,
            "applied_at": applied_at,
        }

    @staticmethod
    def _delimiter(text: str) -> str:
        first = next((line for line in text.splitlines() if line.strip()), "")
        scores = {delimiter: first.count(delimiter) for delimiter in (";", "\t", ",")}
        return max(scores, key=scores.get) if scores and max(scores.values()) > 0 else ";"

    def preview_ric_import(self, raw_text: Any) -> dict[str, Any]:
        text = str(raw_text or "")
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError("Indsæt RIC-data først")
        if len(lines) > self.MAX_IMPORT_LINES:
            raise ValueError("For mange RIC-linjer i én import")

        delimiter = self._delimiter(text)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for line_no, values in enumerate(reader, start=1):
            if not values or not any(str(value).strip() for value in values):
                continue
            ric = str(values[0] if values else "").strip()
            if line_no == 1 and ric.casefold() in {"ric", "capcode", "address", "adresse"}:
                continue
            station_name = str(values[1] if len(values) > 1 else "").strip()
            label = str(values[2] if len(values) > 2 else "").strip()[:120]
            active_raw = str(values[3] if len(values) > 3 else "1").strip().casefold()
            try:
                clean_ric = self.routing.normalize_ric(ric)
                clean_station = self.routing.normalize_station_name(station_name)
            except ValueError as exc:
                errors.append({"line": line_no, "raw": delimiter.join(values), "error": str(exc)})
                continue
            active = active_raw not in {"0", "false", "nej", "no", "off", "inaktiv"}
            rows.append({
                "line": line_no,
                "ric": clean_ric,
                "station": clean_station,
                "label": label,
                "active": active,
                "station_exists": bool(self.routing.station_key(clean_station)),
            })
        return {"delimiter": delimiter, "rows": rows, "errors": errors}

    def apply_ric_import(self, raw_text: Any, create_missing_stations: bool, created_by: int = None) -> dict[str, Any]:
        preview = self.preview_ric_import(raw_text)
        created = 0
        skipped = 0
        failures = list(preview["errors"])
        stations_created = 0
        for row in preview["rows"]:
            station_key = self.routing.station_key(row["station"])
            if not station_key and create_missing_stations:
                station = self.routing.create_station(row["station"], source="ric-bulk-import")
                station_key = station["key"]
                stations_created += 1
            if not station_key:
                failures.append({"line": row["line"], "raw": row["ric"], "error": "Området findes ikke"})
                continue
            with self.routing.connect() as conn:
                existing = conn.execute("SELECT 1 FROM ric_codes WHERE ric=?", (row["ric"],)).fetchone()
            if existing:
                skipped += 1
                continue
            try:
                self.routing.create_ric_code(
                    row["ric"], station_key, row["label"], bool(row["active"]), created_by,
                )
                created += 1
            except (ValueError, sqlite3.IntegrityError) as exc:
                failures.append({"line": row["line"], "raw": row["ric"], "error": str(exc)})
        return {
            "created": created,
            "skipped_existing": skipped,
            "stations_created": stations_created,
            "errors": failures,
        }
