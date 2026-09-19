"""Conservative privacy redaction for SBR Pager payloads.

The original SMS remains available to the legacy SMS-gateway/Vagtbytte flow.
Only data sent onward to SBR Pager/WhatsApp is redacted here.
"""

from __future__ import annotations

import re

REDACTED_CPR = "[CPR SKJULT]"
REDACTED_PHONE = "[TELEFON SKJULT]"
REDACTED_EMAIL = "[EMAIL SKJULT]"
REDACTED_NAME = "[NAVN SKJULT]"

# Explicit person/name fields are intentionally conservative. Free-text names
# are not guessed, because that would risk censoring roads, places and units.
_NAME_FIELD_RE = re.compile(
    r"(?im)^(?P<label>\s*(?:navn|patient|borger|kontaktperson|anmelder|"
    r"forurettede?|tilskadekomne?)\s*[:=]\s*)(?P<value>[^\r\n]+)$"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_CPR_RE = re.compile(r"(?<!\d)(?P<date>\d{6})[ -]?(?P<serial>\d{4})(?!\d)")
_INTERNATIONAL_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+|00)\d{1,3}(?:[ .()/-]*\d){7,12}(?!\d)"
)
_DANISH_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+45[ .-]*|0045[ .-]*)?(?:\d[ .-]*){8}(?!\d)"
)


def _looks_like_cpr(match: re.Match[str]) -> bool:
    """Use a date-shaped first six digits to reduce false positives."""
    value = match.group("date")
    try:
        day = int(value[:2])
        month = int(value[2:4])
    except ValueError:
        return False
    return 1 <= day <= 31 and 1 <= month <= 12


def redact_personal_data(text: str | None) -> str | None:
    if text is None:
        return None

    value = str(text)

    def redact_name(match: re.Match[str]) -> str:
        return f"{match.group('label')}{REDACTED_NAME}"

    value = _NAME_FIELD_RE.sub(redact_name, value)
    value = _EMAIL_RE.sub(REDACTED_EMAIL, value)

    def redact_cpr(match: re.Match[str]) -> str:
        return REDACTED_CPR if _looks_like_cpr(match) else match.group(0)

    value = _CPR_RE.sub(redact_cpr, value)
    value = _INTERNATIONAL_PHONE_RE.sub(REDACTED_PHONE, value)

    # Run Danish 8-digit numbers last. CPR placeholders are already non-numeric,
    # so they cannot be re-matched as phone numbers.
    value = _DANISH_PHONE_RE.sub(REDACTED_PHONE, value)
    return value


if __name__ == "__main__":
    sample = (
        "(S)M+V · Assistance · Brovej 1, 4180 Sorø\n"
        "Patient: Peter Jensen\nCPR: 010190-1234\n"
        "Kontakt: +45 22 27 03 96\nMail: test@example.dk"
    )
    cleaned = redact_personal_data(sample)
    assert "Peter Jensen" not in cleaned
    assert "010190" not in cleaned
    assert "22 27 03 96" not in cleaned
    assert "test@example.dk" not in cleaned
    assert "Brovej 1, 4180 Sorø" in cleaned
