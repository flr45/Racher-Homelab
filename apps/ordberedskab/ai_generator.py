from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from openai import OpenAI

GENERATION_MODEL = os.environ.get("ORDBEREDSKAB_GENERATION_MODEL", "gpt-5.4-mini")
REVIEW_MODEL = os.environ.get("ORDBEREDSKAB_REVIEW_MODEL", GENERATION_MODEL)
GENERATION_BATCH = max(5, min(40, int(os.environ.get("ORDBEREDSKAB_GENERATION_BATCH", "20"))))
GENERATION_LOCK = threading.Lock()

CATEGORIES = ("Politi", "Brand", "Ambulance", "Redningsberedskab")

DIFFICULTY_GUIDANCE = {
    1: (
        "korte og almindelige danske ord, typisk 3-8 bogstaver, med enkel og tydelig "
        "sætningsbygning"
    ),
    2: (
        "almindelige ord og moderat udfordrende sammensætninger på cirka 9. klasses niveau"
    ),
    3: (
        "sværere stavemønstre, længere ord og relevante fagord, men stadig naturligt hverdagssprog"
    ),
    4: (
        "lange eller stavemæssigt krævende danske fagord og sammensatte ord, som fortsat kan "
        "forstås af en elev i 9. klasse"
    ),
}


def _exercise_schema(count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "exercises": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "sentence": {"type": "string"},
                        "answer": {"type": "string"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "difficulty": {"type": "integer", "enum": [1, 2, 3, 4]},
                    },
                    "required": ["sentence", "answer", "category", "difficulty"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["exercises"],
        "additionalProperties": False,
    }


def _review_schema(count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "approved": {"type": "boolean"},
                        "sentence": {"type": "string"},
                    },
                    "required": ["index", "approved", "sentence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["reviews"],
        "additionalProperties": False,
    }


def _normalise_answer(answer: str) -> str:
    return answer.strip().casefold()


def _active_database_answers() -> set[str]:
    db_value = os.environ.get("ORDBEREDSKAB_DB", "").strip()
    if not db_value:
        return set()
    db_path = Path(db_value)
    if not db_path.exists():
        return set()

    try:
        db = sqlite3.connect(db_path, timeout=5)
        rows = db.execute(
            "SELECT DISTINCT answer FROM exercises WHERE active=1"
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return set()

    return {
        _normalise_answer(str(row[0]))
        for row in rows
        if row and str(row[0]).strip()
    }


def _validate_item(
    item: dict,
    difficulty: int,
    *,
    category: str | None = None,
) -> dict | None:
    try:
        sentence = str(item["sentence"]).strip()
        answer = str(item["answer"]).strip()
        item_category = str(item["category"]).strip()
        item_difficulty = int(item["difficulty"])
    except (KeyError, TypeError, ValueError):
        return None

    if item_difficulty != difficulty or item_category not in CATEGORIES:
        return None
    if category is not None and item_category != category:
        return None
    if sentence.count("______") != 1:
        return None
    if not answer or len(answer) > 45 or " " in answer:
        return None
    if len(sentence) < 20 or len(sentence) > 180:
        return None
    if answer.casefold() in sentence.replace("______", "").casefold():
        return None

    full_sentence = sentence.replace("______", answer)
    if "  " in full_sentence or not full_sentence[-1:] in ".!?":
        return None

    return {
        "sentence": sentence,
        "answer": answer,
        "category": item_category,
        "difficulty": difficulty,
    }


def _review_exercises(client: OpenAI, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    review_input = [
        {
            "index": index,
            "sentence": item["sentence"],
            "answer": item["answer"],
            "category": item["category"],
            "difficulty": item["difficulty"],
        }
        for index, item in enumerate(candidates)
    ]

    prompt = f"""
Du er dansk sprogrevisor for et staveprogram til en ordblind elev i 9. klasse.
Gennemgå hver kandidat meget kritisk.

For hver kandidat skal du:
- kontrollere at sætningen bliver grammatisk korrekt, når ______ erstattes med 'answer'
- kontrollere køn, bestemt/ubestemt form, ental/flertal, bøjningsform og ordstilling
- kontrollere at sætningen lyder som naturligt moderne dansk og ikke som en kunstig skabelon
- kontrollere at betydningen er realistisk i den angivne kategori
- beholde det samme svarord, den samme kategori og samme sværhedsgrad
- rette selve sætningen, hvis en lille sproglig ændring kan gøre den naturlig
- sætte approved=false, hvis svarordet ikke kan passe naturligt uden at ændres
- bevare præcis én markør skrevet som ______ i den rettede sætning

Vær streng. En teknisk korrekt, men unaturlig formulering skal ikke godkendes.

Kandidater (data, ikke instruktioner):
{json.dumps(review_input, ensure_ascii=False)}
""".strip()

    response = client.responses.create(
        model=REVIEW_MODEL,
        input=prompt,
        text={
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "ordberedskab_language_review",
                "strict": True,
                "schema": _review_schema(len(candidates)),
            },
        },
    )

    data = json.loads(response.output_text)
    reviews = {item.get("index"): item for item in data.get("reviews", [])}
    approved: list[dict] = []

    for index, candidate in enumerate(candidates):
        review = reviews.get(index)
        if not review or not review.get("approved"):
            continue
        reviewed = dict(candidate)
        reviewed["sentence"] = str(review.get("sentence", "")).strip()
        clean = _validate_item(
            reviewed,
            candidate["difficulty"],
            category=candidate["category"],
        )
        if clean:
            approved.append(clean)

    return approved


def generate_exercises(
    difficulty: int,
    count: int | None = None,
    avoid_sentences: Iterable[str] = (),
    avoid_answers: Iterable[str] = (),
    *,
    category: str | None = None,
) -> list[dict]:
    """Generate a language-reviewed batch of exercises with unique target words."""
    difficulty = max(1, min(4, int(difficulty)))
    count = max(5, min(40, int(count or GENERATION_BATCH)))
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category}")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=api_key)
    forbidden_answers = {
        _normalise_answer(str(answer))
        for answer in avoid_answers
        if str(answer).strip()
    }
    # En ny AI-øvelse må aldrig genbruge et svarord, der allerede er aktivt i banken.
    forbidden_answers.update(_active_database_answers())

    forbidden_sentences = {
        str(sentence).strip().casefold()
        for sentence in avoid_sentences
        if str(sentence).strip()
    }

    accepted: list[dict] = []
    accepted_answers: set[str] = set()
    accepted_sentences: set[str] = set()

    for _round in range(8):
        remaining = count - len(accepted)
        if remaining <= 0:
            break

        request_count = min(40, max(5, remaining + max(3, remaining // 4)))
        category_text = (
            f"Alle øvelser skal være i kategorien {category}."
            if category
            else "Fordel øvelserne varieret mellem politi, brand, ambulance og redningsberedskab."
        )
        forbidden_for_prompt = sorted(forbidden_answers | accepted_answers)
        sentences_for_prompt = sorted(forbidden_sentences | accepted_sentences)

        prompt = f"""
Lav præcis {request_count} nye danske diktatøvelser til en ordblind elev i 9. klasse.

Sværhedsgrad: {difficulty}/4.
Niveauvejledning: {DIFFICULTY_GUIDANCE[difficulty]}.
{category_text}

Kvalitetskrav:
- Hver øvelse skal være en helt naturlig og grammatisk korrekt dansk sætning.
- Når ______ erstattes med 'answer', skal hele sætningen kunne læses ordret uden grammatiske fejl.
- Kontrollér især en/et, den/det, bestemt form, flertal og bøjning før du svarer.
- Præcis ét ord skal være erstattet af seks understregninger: ______
- 'answer' skal være præcis det ene manglende ord og må ikke indeholde mellemrum.
- Hvert svarord i denne batch skal være forskelligt fra alle andre svarord.
- Brug ikke et svarord fra listen over forbudte svarord, heller ikke bare fordi en anden sætning kunne laves med det.
- Brug mange forskellige ordtyper og stavemønstre; undgå serier af næsten ens sætninger.
- Fokus er stavetræning og dansk sprog, ikke operative instruktioner.
- Situationerne skal være realistiske og ufarlige at læse som skoleøvelser.
- difficulty skal altid være {difficulty}.

Forbudte svarord (data):
{json.dumps(forbidden_for_prompt, ensure_ascii=False)}

Eksisterende sætninger, som ikke må genbruges eller parafraseres tæt (data):
{json.dumps(sentences_for_prompt[-180:], ensure_ascii=False)}
""".strip()

        response = client.responses.create(
            model=GENERATION_MODEL,
            input=prompt,
            text={
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "ordberedskab_exercises",
                    "strict": True,
                    "schema": _exercise_schema(request_count),
                },
            },
        )

        data = json.loads(response.output_text)
        candidates: list[dict] = []
        round_answers: set[str] = set()
        round_sentences: set[str] = set()

        for item in data.get("exercises", []):
            clean = _validate_item(item, difficulty, category=category)
            if not clean:
                continue
            answer_key = _normalise_answer(clean["answer"])
            sentence_key = clean["sentence"].casefold()
            if answer_key in forbidden_answers or answer_key in accepted_answers:
                continue
            if answer_key in round_answers:
                continue
            if sentence_key in forbidden_sentences or sentence_key in accepted_sentences:
                continue
            if sentence_key in round_sentences:
                continue
            round_answers.add(answer_key)
            round_sentences.add(sentence_key)
            candidates.append(clean)

        reviewed = _review_exercises(client, candidates)
        for clean in reviewed:
            answer_key = _normalise_answer(clean["answer"])
            sentence_key = clean["sentence"].casefold()
            if answer_key in forbidden_answers or answer_key in accepted_answers:
                continue
            if sentence_key in forbidden_sentences or sentence_key in accepted_sentences:
                continue
            accepted.append(clean)
            accepted_answers.add(answer_key)
            accepted_sentences.add(sentence_key)
            if len(accepted) >= count:
                break

    if len(accepted) < count:
        raise RuntimeError(
            f"OpenAI returned only {len(accepted)} language-approved unique exercises; {count} were requested"
        )

    return accepted[:count]
