from __future__ import annotations

import json
import os
import threading
from typing import Iterable

from openai import OpenAI

GENERATION_MODEL = os.environ.get("ORDBEREDSKAB_GENERATION_MODEL", "gpt-5.4-mini")
GENERATION_BATCH = max(5, min(40, int(os.environ.get("ORDBEREDSKAB_GENERATION_BATCH", "20"))))
GENERATION_LOCK = threading.Lock()

CATEGORIES = ("Politi", "Brand", "Ambulance", "Redningsberedskab")

DIFFICULTY_GUIDANCE = {
    1: "korte, almindelige ord og enkle sætninger på cirka 7.-8. klasses læseniveau",
    2: "almindelige og moderat udfordrende ord på cirka 9. klasses niveau",
    3: "sværere fagord, sammensatte ord og længere sætninger på 9.-10. klasses niveau",
    4: "udfordrende danske fagord og sammensatte ord, men stadig realistiske og forståelige for en elev i 9. klasse",
}


def _schema(count: int) -> dict:
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


def _validate_item(item: dict, difficulty: int) -> dict | None:
    try:
        sentence = str(item["sentence"]).strip()
        answer = str(item["answer"]).strip()
        category = str(item["category"]).strip()
        item_difficulty = int(item["difficulty"])
    except (KeyError, TypeError, ValueError):
        return None

    if item_difficulty != difficulty or category not in CATEGORIES:
        return None
    if sentence.count("______") != 1:
        return None
    if not answer or len(answer) > 40 or " " in answer.strip():
        return None
    if len(sentence) < 20 or len(sentence) > 180:
        return None
    if answer.casefold() in sentence.replace("______", "").casefold():
        return None

    return {
        "sentence": sentence,
        "answer": answer,
        "category": category,
        "difficulty": difficulty,
    }


def generate_exercises(
    difficulty: int,
    count: int | None = None,
    avoid_sentences: Iterable[str] = (),
    avoid_answers: Iterable[str] = (),
) -> list[dict]:
    """Generate a validated batch of new exercises with OpenAI."""
    difficulty = max(1, min(4, int(difficulty)))
    count = max(5, min(40, int(count or GENERATION_BATCH)))

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    avoid_sentences_list = list(avoid_sentences)[-120:]
    avoid_answers_list = list(avoid_answers)[-80:]

    prompt = f"""
Lav præcis {count} nye danske diktatøvelser til en ordblind elev i 9. klasse.

Sværhedsgrad: {difficulty}/4.
Niveauvejledning: {DIFFICULTY_GUIDANCE[difficulty]}.

Krav:
- Hver øvelse skal være en naturlig, grammatisk korrekt dansk sætning.
- Præcis ét ord skal være erstattet af seks understregninger: ______
- 'answer' skal være præcis det ene manglende ord.
- Undgå bindestregsvar og svar med mellemrum.
- Sætningerne skal relatere sig til politi, brand, ambulance eller redningsberedskab.
- Cirka 60 % skal handle om politi. Resten fordeles mellem de tre øvrige kategorier.
- Fokus er stavetræning, ikke operative instruktioner.
- Brug realistiske danske ord og situationer.
- Lav forskellige formuleringer og undgå at gentage de samme svarord unødigt.
- difficulty skal altid være {difficulty}.

Undgå så vidt muligt disse eksisterende sætninger:
{json.dumps(avoid_sentences_list, ensure_ascii=False)}

Undgå så vidt muligt disse nyligt brugte svarord:
{json.dumps(avoid_answers_list, ensure_ascii=False)}
""".strip()

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=GENERATION_MODEL,
        input=prompt,
        text={
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "ordberedskab_exercises",
                "strict": True,
                "schema": _schema(count),
            },
        },
    )

    data = json.loads(response.output_text)
    validated: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in data.get("exercises", []):
        clean = _validate_item(item, difficulty)
        if not clean:
            continue
        key = (clean["sentence"].casefold(), clean["answer"].casefold())
        if key in seen:
            continue
        seen.add(key)
        validated.append(clean)

    if not validated:
        raise RuntimeError("OpenAI returned no valid exercises")
    return validated
