from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from ai_generator import CATEGORIES, generate_exercises

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ORDBEREDSKAB_DB", BASE_DIR / "ordberedskab.db"))
SEED_DIR = BASE_DIR / "seed"
REVISION = 2

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


def generate_bank(
    custom_rows: list[sqlite3.Row],
) -> list[dict]:
    used_answers = {
        str(row["answer"]).strip().casefold()
        for row in custom_rows
        if str(row["answer"]).strip()
    }
    used_sentences = {
        str(row["sentence"]).strip().casefold()
        for row in custom_rows
        if str(row["sentence"]).strip()
    }

    bank: list[dict] = []
    total_groups = len(CATEGORIES) * len(LEVEL_QUOTAS)
    group_number = 0

    for category in CATEGORIES:
        for difficulty, quota in LEVEL_QUOTAS.items():
            group_number += 1
            print(
                f"[{group_number}/{total_groups}] Genererer {quota} · {category} · niveau {difficulty}",
                flush=True,
            )
            items = generate_exercises(
                difficulty=difficulty,
                count=quota,
                category=category,
                avoid_answers=used_answers,
                avoid_sentences=used_sentences,
            )
            for item in items:
                answer_key = item["answer"].strip().casefold()
                sentence_key = item["sentence"].strip().casefold()
                if answer_key in used_answers:
                    raise RuntimeError(f"Duplicate answer slipped through: {item['answer']}")
                if sentence_key in used_sentences:
                    raise RuntimeError("Duplicate sentence slipped through")
                used_answers.add(answer_key)
                used_sentences.add(sentence_key)
                bank.append(item)

    if len(bank) != 500:
        raise RuntimeError(f"Expected 500 exercises, generated {len(bank)}")

    answers = [item["answer"].strip().casefold() for item in bank]
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
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("OPENAI_API_KEY mangler i containeren.")

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
    print("Genererer først hele den nye bank. Databasen ændres først, når alle 500 er godkendt.\n")

    bank = generate_bank(custom_rows)
    archived, inserted = install_bank(db, legacy, bank)

    active = db.execute(
        "SELECT COUNT(*) FROM exercises WHERE active=1"
    ).fetchone()[0]
    unique_answers = db.execute(
        "SELECT COUNT(DISTINCT lower(answer)) FROM exercises WHERE active=1"
    ).fetchone()[0]

    print("\n===== FÆRDIG =====")
    print(f"Gamle seed-rækker arkiveret: {archived}")
    print(f"Nye kvalitetsøvelser indsat: {inserted}")
    print(f"Aktive øvelser i alt: {active}")
    print(f"Forskellige aktive svarord: {unique_answers}")
    print("Eksisterende historik på gamle øvelser er bevaret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
