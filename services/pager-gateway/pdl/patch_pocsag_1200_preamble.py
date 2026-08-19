#!/usr/bin/env python3
"""Add privacy-safe POCSAG-1200 preamble diagnostics to pinned PDL.

This patch only adds metadata telemetry around the existing 1200-baud preamble
counter. It does not change timing thresholds, polarity, decoder state, raw
symbol handling, RIC/capcodes or message payloads.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


MARKER = "RACHER_POCSAG_1200_PREAMBLE_DIAG"


def sub_once(text: str, pattern: str, replacement, label: str, *, flags: int = 0) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"Could not patch {label}: expected 1 match, found {count}")
    return updated


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <PDL source directory>", file=sys.stderr)
        return 2

    source = Path(sys.argv[1]).resolve()
    decode_path = source / "decode.cpp"
    if not decode_path.is_file():
        raise RuntimeError(f"Missing {decode_path}")

    decode = decode_path.read_text(encoding="utf-8")
    if MARKER in decode:
        print("POCSAG-1200 preamble diagnostics already applied")
        return 0

    if "#include <stdio.h>" not in decode or "#include <stdlib.h>" not in decode:
        decode = sub_once(
            decode,
            r"^#include <windows\.h>\s*$",
            "#include <windows.h>\n#include <stdio.h>\n#include <stdlib.h>",
            "1200 preamble diagnostic includes",
            flags=re.MULTILINE,
        )

    preamble_state = r'''
/* RACHER_POCSAG_1200_PREAMBLE_DIAG: metadata-only visibility into the
 * existing 1200-baud preamble detector. No raw symbols or message data. */
static int s_pocsag_1200_preamble_enabled = -1;
static int s_pocsag_1200_preamble_bucket = 0;
static unsigned long s_pocsag_1200_preamble_attempt = 0;

static int pocsag_1200_preamble_diag_enabled(void)
{
	if (s_pocsag_1200_preamble_enabled < 0) {
		const char *env = getenv("PDL_POCSAG_1200_DIAG");
		s_pocsag_1200_preamble_enabled = (env && env[0] && env[0] != '0') ? 1 : 0;
	}
	return s_pocsag_1200_preamble_enabled;
}

static void pocsag_1200_preamble_note(int count, int interval)
{
	if (!pocsag_1200_preamble_diag_enabled()) return;

	if (count < 10) {
		s_pocsag_1200_preamble_bucket = 0;
		return;
	}

	if (count > 180) {
		if (s_pocsag_1200_preamble_bucket == 0)
			s_pocsag_1200_preamble_attempt++;
		fprintf(stderr,
			"[POCSAG-PREAMBLE] baud=1200 attempt=%lu stage=acquired count=%d interval=%d acquired=1\n",
			s_pocsag_1200_preamble_attempt, count, interval);
		fflush(stderr);
		s_pocsag_1200_preamble_bucket = 0;
		return;
	}

	int bucket = count / 30;
	if (bucket > 6) bucket = 6;
	if (bucket <= 0 || bucket <= s_pocsag_1200_preamble_bucket) return;

	if (s_pocsag_1200_preamble_bucket == 0)
		s_pocsag_1200_preamble_attempt++;
	s_pocsag_1200_preamble_bucket = bucket;

	fprintf(stderr,
		"[POCSAG-PREAMBLE] baud=1200 attempt=%lu stage=%d count=%d interval=%d acquired=0\n",
		s_pocsag_1200_preamble_attempt, bucket * 30, count, interval);
	fflush(stderr);
}
'''

    decode = sub_once(
        decode,
        r"^(?P<decl>\s*int\s+pocbit\s*=\s*0\s*;[^\n]*\n)",
        lambda match: match.group("decl") + preamble_state + "\n",
        "1200 preamble diagnostic state",
        flags=re.MULTILINE,
    )

    counter_pattern = (
        r"(?P<counter>^[ \t]*if\s*\(\(pd_dinc\s*>\s*842\)\s*&&\s*"
        r"\(pd_dinc\s*<\s*1142\)\)\s*pd_ct12\+\+;\s*\n"
        r"^[ \t]*else\s+if\s*\(pd_ct12\s*>\s*5\)\s*pd_ct12\s*-=\s*3;\s*\n)"
    )
    decode = sub_once(
        decode,
        counter_pattern,
        lambda match: match.group("counter") + "\t\t\tpocsag_1200_preamble_note(pd_ct12, pd_dinc);\n",
        "1200 preamble counter visibility",
        flags=re.MULTILINE,
    )

    decode_path.write_text(decode, encoding="utf-8")
    print(f"Applied whitespace-safe POCSAG-1200 preamble diagnostics to {decode_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
