from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from ai_generator import CATEGORIES, generate_exercises

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ORDBEREDSKAB_DB", BASE_DIR / "ordberedskab.db"))
SEED_DIR = BASE_DIR / "seed"
REVISION = 2
CHECKPOINT_PATH = DB_PATH.parent / f"quality-bank-build-v{REVISION}.json"
CHUNK_SIZE = 10
MAX_CHUNK_ATTEMPTS = 6

CATEGORY_BY_FILE = {
    "politi.tsv": "Politi",
    "brand.tsv": "Brand",
    "ambulance.tsv": "Ambulance",
    "redningsberedskab.tsv": "Redningsberedskab",
}

# 31 + 31 + 32 + 31 = 125 pr. kategori = 500 i alt.
LEVEL_QUOTAS = {1: 31, 2: 31, 3: 32, 4: 31}


def legacy_seed_rows() -> set[tuple[str, str, str, int]]:
    rows: set[tuple[str, str, str, int]] = set()
    for filename, category in CATEGORY_BY_FILE.items():
        path = SEED_DIR / filename
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            parts = raw_line.split("\t", 2)
            if len(parts) != 3:
                continue
            raw_level, answer, sentence = parts
            try:
                level = int(raw_level)
            except ValueError:
                continue
            rows.add((sentence.strip(), answer.strip(), category, level))
    return rows


def setup_metadata(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quality_bank_exercises (
            exercise_id INTEGER PRIMARY KEY,
            revision INTEGER NOT NULL,
            FOREIGN KEY(exercise_id) REFERENCES exercises(id) ON DELETE CASCADE
        );
        """
    )
    db.commit()


def current_revision(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT value FROM app_meta WHERE key='quality_bank_revision'"
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def custom_active_rows(
    db: sqlite3.Connection,
    legacy: set[tuple[str, str, str, int]],
) -> list[sqlite3.Row]:
    managed_ids = {
        row[0]
        for row in db.execute(
            "SELECT exercise_id FROM quality_bank_exercises"
        ).fetchall()
    }
    rows = db.execute(
        "SELECT id,sentence,answer,category,difficulty FROM exercises WHERE active=1"
    ).fetchall()
    result = []
    for row in rows:
        signature = (
            row["sentence"],
            row["answer"],
            row["category"],
            int(row["difficulty"]),
        )
        if signature in legacy or row["id"] in managed_ids:
            continue
        result.append(row)
    return result


def _normalise_answer(value: str) -> str:
    return value.strip().casefold()


def _normalise_sentence(value: str) -> str:
    return value.strip().casefold()


def _validate_checkpoint_item(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        sentence = str(item["sentence"]).strip()
        answer = str(item["answer"]).strip()
        category = str(item["category"]).strip()
        difficulty = int(item["difficulty"])
    except (KeyError, TypeError, ValueError):
        return None
    if category not in CATEGORIES or difficulty not in LEVEL_QUOTAS:
        return None
    if sentence.count("______") != 1 or not answer:
        return None
    return {
        "sentence": sentence,
        "answer": answer,
        "category": category,
        "difficulty": difficulty,
    }


def load_checkpoint(custom_rows: list[sqlite3.Row]) -> list[dict]:
    if not CHECKPOINT_PATH.exists():
        return []

    try:
        payload = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Checkpoint-filen {CHECKPOINT_PATH} kan ikke læses: {exc}"
        ) from exc

    if payload.get("revision") != REVISION:
        return []

    custom_answers = {
        _normalise_answer(str(row["answer"]))
        for row in custom_rows
        if str(row["answer"]).strip()
    }
    custom_sentences = {
        _normalise_sentence(str(row["sentence"]))
        for row in custom_rows
        if str(row["sentence"]).strip()
    }

    bank: list[dict] = []
    seen_answers = set(custom_answers)
    seen_sentences = set(custom_sentences)
    group_counts = {
        (category, difficulty): 0
        for category in CATEGORIES
        for difficulty in LEVEL_QUOTAS
    }

    for raw_item in payload.get("exercises", []):
        item = _validate_checkpoint_item(raw_item)
        if not item:
            continue
        key = (item["category"], item["difficulty"])
        if group_counts[key] >= LEVEL_QUOTAS[item["difficulty"]]:
            continue
        answer_key = _normalise_answer(item["answer"])
        sentence_key = _normalise_sentence(item["sentence"])
        if answer_key in seen_answers or sentence_key in seen_sentences:
            continue
        seen_answers.add(answer_key)
        seen_sentences.add(sentence_key)
        group_counts[key] += 1
        bank.append(item)

    print(
        f"Genoptager checkpoint: {len(bank)} godkendte sætninger fundet i {CHECKPOINT_PATH}",
        flush=True,
    )
    return bank


def save_checkpoint(bank: list[dict]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CHECKPOINT_PATH.with_suffix(".tmp")
    payload = {
        "revision": REVISION,
        "exercises": bank,
    }
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, CHECKPOINT_PATH)


def generate_bank(
    custom_rows: list[sqlite3.Row],
) -> list[dict]:
    used_answers = {
        _normalise_answer(str(row["answer"]))
        for row in custom_rows
        if str(row["answer"]).strip()
    }
    used_sentences = {
        _normalise_sentence(str(row["sentence"]))
        for row in custom_rows
        if str(row["sentence"]).strip()
    }

    bank = load_checkpoint(custom_rows)
    for item in bank:
        used_answers.add(_normalise_answer(item["answer"]))
        used_sentences.add(_normalise_sentence(item["sentence"]))

    total_groups = len(CATEGORIES) * len(LEVEL_QUOTAS)
    group_number = 0

    for category in CATEGORIES:
        for difficulty, quota in LEVEL_QUOTAS.items():
            group_number += 1
            existing = sum(
                1
                for item in bank
                if item["category"] == category and item["difficulty"] == difficulty
            )
            remaining = quota - existing

            if remaining <= 0:
                print(
                    f"[{group_number}/{total_groups}] Klar fra checkpoint · {category} · niveau {difficulty} ({quota}/{quota})",
                    flush=True,
                )
                continue

            print(
                f"[{group_number}/{total_groups}] {category} · niveau {difficulty}: "
                f"har {existing}/{quota}, mangler {remaining}",
                flush=True,
            )

            while remaining > 0:
                wanted = min(CHUNK_SIZE, remaining)
                # Generatoren kræver mindst 5. Ved en rest på 1-4 genererer vi 5
                # godkendte kandidater og beholder kun det nødvendige antal.
                request_count = max(5, wanted)
                last_error: Exception | None = None
                items: list[dict] | None = None

                for attempt in range(1, MAX_CHUNK_ATTEMPTS + 1):
                    print(
                        f"    Delbatch: mangler {remaining}; bestiller {request_count} "
                        f"(forsøg {attempt}/{MAX_CHUNK_ATTEMPTS})",
                        flush=True,
                    )
                    try:
                        items = generate_exercises(
                            difficulty=difficulty,
                            count=request_count,
                            category=category,
                            avoid_answers=used_answers,
                            avoid_sentences=used_sentences,
                        )
                        break
                    except RuntimeError as exc:
                        last_error = exc
                        print(f"      Afvist/ufuldstændig batch: {exc}", flush=True)

                if items is None:
                    raise RuntimeError(
                        f"Kunne ikke samle en godkendt delbatch for {category}, niveau {difficulty} "
                        f"efter {MAX_CHUNK_ATTEMPTS} forsøg. Checkpoint er gemt i {CHECKPOINT_PATH}."
                    ) from last_error

                accepted_now = 0
                for item in items:
                    if accepted_now >= wanted:
                        break
                    answer_key = _normalise_answer(item["answer"])
                    sentence_key = _normalise_sentence(item["sentence"])
                    if answer_key in used_answers or sentence_key in used_sentences:
                        continue
                    used_answers.add(answer_key)
                    used_sentences.add(sentence_key)
                    bank.append(item)
                    accepted_now += 1

                if accepted_now != wanted:
                    raise RuntimeError(
                        f"Delbatch gav kun {accepted_now}/{wanted} brugbare unikke øvelser. "
                        f"Checkpoint er gemt i {CHECKPOINT_PATH}."
                    )

                remaining -= accepted_now
                save_checkpoint(bank)
                print(
                    f"      Gemte {accepted_now}; gruppe nu {quota - remaining}/{quota}; "
                    f"checkpoint total {len(bank)}/500",
                    flush=True,
                )

    if len(bank) != 500:
        raise RuntimeError(f"Expected 500 exercises, generated {len(bank)}")

    answers = [_normalise_answer(item["answer"]) for item in bank]
    if len(set(answers)) != len(answers):
        raise RuntimeError("Generated bank contains duplicate target words")

    return bank


def install_bank(
    db: sqlite3.Connection,
    legacy: set[tuple[str, str, str, int]],
    bank: list[dict],
) -> tuple[int, int]:
    previous_managed_ids = [
        row[0]
        for row in db.execute(
            "SELECT exercise_id FROM quality_bank_exercises"
        ).fetchall()
    ]

    archived = 0
    inserted = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        # Gem de gamle rækker og historikken, men tag dem ud af den aktive bank.
        for sentence, answer, category, difficulty in legacy:
            cursor = db.execute(
                """
                UPDATE exercises
                SET active=0
                WHERE active=1 AND sentence=? AND answer=? AND category=? AND difficulty=?
                """,
                (sentence, answer, category, difficulty),
            )
            archived += cursor.rowcount

        if previous_managed_ids:
            placeholders = ",".join("?" for _ in previous_managed_ids)
            db.execute(
                f"UPDATE exercises SET active=0 WHERE id IN ({placeholders})",
                previous_managed_ids,
            )

        db.execute("DELETE FROM quality_bank_exercises")

        for item in bank:
            cursor = db.execute(
                """
                INSERT INTO exercises (sentence,answer,category,difficulty,active)
                VALUES (?,?,?,?,1)
                """,
                (
                    item["sentence"],
                    item["answer"],
                    item["category"],
                    item["difficulty"],
                ),
            )
            db.execute(
                "INSERT INTO quality_bank_exercises (exercise_id,revision) VALUES (?,?)",
                (cursor.lastrowid, REVISION),
            )
            inserted += 1

        db.execute(
            """
            INSERT INTO app_meta (key,value)
            VALUES ('quality_bank_revision',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(REVISION),),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return archived, inserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild Ordberedskab with 500 unique, language-reviewed exercises."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Generate a fresh revision even if revision 2 is already installed.",
    )
    parser.add_argument(
        "--restart-build",
        action="store_true",
        help="Discard an unfinished checkpoint and generate the 500 exercises from scratch.",
    )
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("OPENAI_API_KEY mangler i containeren.")

    if args.restart_build and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print(f"Slettede tidligere checkpoint: {CHECKPOINT_PATH}")

    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 30000")
    setup_metadata(db)

    revision = current_revision(db)
    if revision >= REVISION and not args.force:
        active = db.execute(
            "SELECT COUNT(*) FROM exercises WHERE active=1"
        ).fetchone()[0]
        unique_answers = db.execute(
            "SELECT COUNT(DISTINCT lower(answer)) FROM exercises WHERE active=1"
        ).fetchone()[0]
        print(
            f"Kvalitetsbank revision {revision} er allerede installeret. "
            f"Aktive sætninger: {active}; forskellige svarord: {unique_answers}."
        )
        return 0

    legacy = legacy_seed_rows()
    custom_rows = custom_active_rows(db, legacy)
    print(f"Gamle bundled seed-rækker fundet i manifest: {len(legacy)}")
    print(f"Eksisterende egne/andre aktive øvelser bevares: {len(custom_rows)}")
    print(
        "Genererer først hele den nye bank. Databasen ændres først, når alle 500 er godkendt."
    )
    print(f"Checkpoint gemmes løbende i: {CHECKPOINT_PATH}\n")

    bank = generate_bank(custom_rows)
    archived, inserted = install_bank(db, legacy, bank)

    active = db.execute(
        "SELECT COUNT(*) FROM exercises WHERE active=1"
    ).fetchone()[0]
    unique_answers = db.execute(
        "SELECT COUNT(DISTINCT lower(answer)) FROM exercises WHERE active=1"
    ).fetchone()[0]

    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()

    print("\n===== FÆRDIG =====")
    print(f"Gamle seed-rækker arkiveret: {archived}")
    print(f"Nye kvalitetsøvelser indsat: {inserted}")
    print(f"Aktive øvelser i alt: {active}")
    print(f"Forskellige aktive svarord: {unique_answers}")
    print("Eksisterende historik på gamle øvelser er bevaret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
