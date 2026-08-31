from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from flask import Blueprint, current_app, send_file
from openai import OpenAI

from app import get_db, login_required

BASE_DIR = Path(__file__).resolve().parent
TTS_CACHE_DIR = Path(os.environ.get("ORDBEREDSKAB_TTS_CACHE", BASE_DIR / "tts-cache"))
TTS_MODEL = os.environ.get("ORDBEREDSKAB_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("ORDBEREDSKAB_TTS_VOICE", "marin")
TTS_NORMAL_SPEED = float(os.environ.get("ORDBEREDSKAB_TTS_SPEED_NORMAL", "0.96"))
TTS_SLOW_SPEED = float(os.environ.get("ORDBEREDSKAB_TTS_SPEED_SLOW", "0.72"))
TTS_INSTRUCTIONS = os.environ.get(
    "ORDBEREDSKAB_TTS_INSTRUCTIONS",
    (
        "Tal på naturligt dansk med tydelig udtale og roligt, venligt tonefald. "
        "Læs hele sætningen præcist som skrevet. Udtal fagord fra politi, brand, "
        "ambulance og redningsberedskab tydeligt. Undgå overdrevet dramatik."
    ),
)
TTS_CACHE_LOCK = threading.Lock()

tts_bp = Blueprint("tts", __name__)
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def full_exercise_sentence(exercise) -> str:
    return exercise["sentence"].replace("______", exercise["answer"])


def tts_settings(mode: str) -> tuple[float, str]:
    if mode == "slow":
        return (
            TTS_SLOW_SPEED,
            TTS_INSTRUCTIONS
            + " Tal langsommere end normalt og lav små naturlige pauser, men stav ikke ordet bogstav for bogstav.",
        )
    return TTS_NORMAL_SPEED, TTS_INSTRUCTIONS


def cache_path(text: str, mode: str) -> Path:
    speed, instructions = tts_settings(mode)
    material = "\n".join((TTS_MODEL, TTS_VOICE, str(speed), instructions, text))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return TTS_CACHE_DIR / f"{digest}.mp3"


def generate_tts_file(text: str, mode: str) -> Path:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    speed, instructions = tts_settings(mode)
    target = cache_path(text, mode)
    if target.exists() and target.stat().st_size > 0:
        return target

    with TTS_CACHE_LOCK:
        if target.exists() and target.stat().st_size > 0:
            return target

        TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_suffix(".tmp")
        client = OpenAI(api_key=api_key)
        try:
            with client.audio.speech.with_streaming_response.create(
                model=TTS_MODEL,
                voice=TTS_VOICE,
                input=text,
                instructions=instructions,
                response_format="mp3",
                speed=speed,
            ) as response:
                response.stream_to_file(temp_path)
            os.replace(temp_path, target)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    return target


@tts_bp.route("/audio/<int:exercise_id>/<mode>.mp3")
@login_required
def exercise_audio(exercise_id: int, mode: str):
    if mode not in {"normal", "slow"}:
        return {"ok": False, "message": "Ukendt oplæsningstype."}, 404

    exercise = get_db().execute(
        "SELECT * FROM exercises WHERE id=? AND active=1",
        (exercise_id,),
    ).fetchone()
    if not exercise:
        return {"ok": False, "message": "Øvelsen findes ikke."}, 404

    text = full_exercise_sentence(exercise)
    try:
        audio_path = generate_tts_file(text, mode)
    except Exception:
        current_app.logger.exception("Kunne ikke generere OpenAI TTS for exercise_id=%s", exercise_id)
        # Returnér teksten kun ved fejl, så browserens indbyggede danske stemme
        # kan bruges som fallback. Ved normal drift sendes svarordet aldrig til JS.
        return {
            "ok": False,
            "message": "AI-oplæsningen er midlertidigt utilgængelig.",
            "fallback_text": text,
        }, 503

    return send_file(
        audio_path,
        mimetype="audio/mpeg",
        conditional=True,
        max_age=0,
    )
