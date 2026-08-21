from __future__ import annotations

import re
from typing import Any


# This filter is intentionally conservative. Real dispatches can contain pager
# prefixes, abbreviations and punctuation, so normal operational structure always
# wins over the generic gibberish heuristic below.
_ALARM_STRUCTURE_RE = re.compile(
    r"(?:\b(?:BRAND(?:ALARM)?|ALARM|ISL|VSBV|ØF|VCT)\b|M\+[SV]|\b\d{4}\s+[A-Za-zÆØÅæøå])",
    re.I,
)
_ALPHA_RUN_RE = re.compile(r"[A-Za-zÆØÅæøå]+")
_HARD_NOISE_CHARS = frozenset("`~^|{}[]\\_<>=&")


def _case_transitions(token: str) -> int:
    letters = [char for char in token if char.isalpha()]
    return sum(
        1
        for left, right in zip(letters, letters[1:])
        if left.isupper() != right.isupper()
    )


def decoder_gibberish_reason(message: Any) -> str | None:
    """Return a suppression reason for high-confidence long decoder garbage.

    Short fragments are handled by the existing decoder/alarm quality filters.
    This catches the other observed failure mode: a long alpha payload that looks
    substantial by length but consists of corrupted mixed-case runs and decoder
    punctuation. We require several independent noise signals to avoid hiding a
    genuine free-text alarm.
    """
    value = str(message or "").strip()
    if len(value) < 24:
        return None

    # Known alarm vocabulary, unit notation or a Danish postcode/locality pattern
    # is strong evidence of a real operational payload. Leave it to the existing
    # filters/deduplication even if a few individual characters are corrupt.
    if _ALARM_STRUCTURE_RE.search(value):
        return None

    compact = "".join(char for char in value if not char.isspace())
    if not compact:
        return None

    hard_noise = sum(char in _HARD_NOISE_CHARS for char in compact)
    question_marks = compact.count("?")
    at_signs = compact.count("@")

    runs = _ALPHA_RUN_RE.findall(value)
    strongly_mixed_runs = 0
    long_mixed_run = False
    for token in runs:
        transitions = _case_transitions(token)
        if len(token) >= 6 and transitions >= 3:
            strongly_mixed_runs += 1
        if len(token) >= 12 and transitions >= 5:
            long_mixed_run = True

    # Multiple decoder punctuation artefacts plus unnatural case switching is a
    # very strong signature of a corrupted POCSAG alpha payload. A legitimate
    # leading @5/$9 prefix alone can never satisfy these conditions.
    punctuation_score = hard_noise + question_marks + max(0, at_signs - 1)
    if long_mixed_run and punctuation_score >= 3:
        return "decoder-gibberish"
    if strongly_mixed_runs >= 2 and punctuation_score >= 4:
        return "decoder-gibberish"
    if hard_noise >= 3 and question_marks >= 2 and at_signs >= 2:
        return "decoder-gibberish"
    return None


def install_gibberish_filter(core: Any):
    """Mark only live PDL gibberish as decoder noise before normal ingestion."""
    original_ingest = core.ingest_event

    def ingest_without_gibberish(event: Any) -> int:
        source = str(getattr(event, "source", "") or "").lower()
        if source.startswith("pdl") and not getattr(event, "decoder_noise_reason", None):
            reason = decoder_gibberish_reason(getattr(event, "message", ""))
            if reason:
                event.decoder_noise_reason = reason
        return original_ingest(event)

    core.ingest_event = ingest_without_gibberish
    return ingest_without_gibberish
