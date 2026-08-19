#!/usr/bin/env python3
"""Add privacy-safe POCSAG preamble diagnostics to pinned PDL.

This patch only adds metadata telemetry around the existing POCSAG preamble
counters. It does not change timing thresholds, polarity, decoder state, raw
symbol handling, RIC/capcodes or message payloads.

PDL_POCSAG_1200_DIAG=1 enables:
- [POCSAG-SCAN] once-per-second max counter visibility for 512/1200/2400.
- [POCSAG-PREAMBLE] 1200-baud checkpoints and acquisition visibility.
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
        print("POCSAG preamble diagnostics already applied")
        return 0

    missing_includes = [
        include
        for include in ("#include <stdio.h>", "#include <stdlib.h>", "#include <time.h>")
        if include not in decode
    ]
    if missing_includes:
        decode = sub_once(
            decode,
            r"^#include <windows\.h>\s*$",
            lambda match: match.group(0) + "\n" + "\n".join(missing_includes),
            "POCSAG scan diagnostic includes",
            flags=re.MULTILINE,
        )

    preamble_state = r'''
/* RACHER_POCSAG_1200_PREAMBLE_DIAG: metadata-only visibility into the
 * existing POCSAG preamble detectors. No raw symbols or message data. */
static int s_pocsag_1200_preamble_enabled = -1;
static int s_pocsag_1200_preamble_bucket = 0;
static unsigned long s_pocsag_1200_preamble_attempt = 0;

static time_t s_pocsag_scan_second = 0;
static unsigned long s_pocsag_scan_samples = 0;
static int s_pocsag_scan_max512 = 0;
static int s_pocsag_scan_max1200 = 0;
static int s_pocsag_scan_max2400 = 0;
static unsigned long s_pocsag_scan_hit512 = 0;
static unsigned long s_pocsag_scan_hit1200 = 0;
static unsigned long s_pocsag_scan_hit2400 = 0;
static int s_pocsag_scan_last_interval = 0;

static int pocsag_1200_preamble_diag_enabled(void)
{
	if (s_pocsag_1200_preamble_enabled < 0) {
		const char *env = getenv("PDL_POCSAG_1200_DIAG");
		s_pocsag_1200_preamble_enabled = (env && env[0] && env[0] != '0') ? 1 : 0;
	}
	return s_pocsag_1200_preamble_enabled;
}

static void pocsag_scan_reset(time_t now)
{
	s_pocsag_scan_second = now;
	s_pocsag_scan_samples = 0;
	s_pocsag_scan_max512 = 0;
	s_pocsag_scan_max1200 = 0;
	s_pocsag_scan_max2400 = 0;
	s_pocsag_scan_hit512 = 0;
	s_pocsag_scan_hit1200 = 0;
	s_pocsag_scan_hit2400 = 0;
	s_pocsag_scan_last_interval = 0;
}

static void pocsag_scan_note(int count512, int count1200, int count2400, int interval)
{
	if (!pocsag_1200_preamble_diag_enabled()) return;

	time_t now = time(NULL);
	if (s_pocsag_scan_second == 0) {
		pocsag_scan_reset(now);
	} else if (now != s_pocsag_scan_second) {
		fprintf(stderr,
			"[POCSAG-SCAN] samples=%lu max512=%d max1200=%d max2400=%d hit512=%lu hit1200=%lu hit2400=%lu last_interval=%d\n",
			s_pocsag_scan_samples,
			s_pocsag_scan_max512, s_pocsag_scan_max1200, s_pocsag_scan_max2400,
			s_pocsag_scan_hit512, s_pocsag_scan_hit1200, s_pocsag_scan_hit2400,
			s_pocsag_scan_last_interval);
		fflush(stderr);
		pocsag_scan_reset(now);
	}

	s_pocsag_scan_samples++;
	if (count512 > s_pocsag_scan_max512) s_pocsag_scan_max512 = count512;
	if (count1200 > s_pocsag_scan_max1200) s_pocsag_scan_max1200 = count1200;
	if (count2400 > s_pocsag_scan_max2400) s_pocsag_scan_max2400 = count2400;
	if (interval > 1976 && interval < 2674) s_pocsag_scan_hit512++;
	if (interval > 842 && interval < 1142) s_pocsag_scan_hit1200++;
	if (interval > 421 && interval < 571) s_pocsag_scan_hit2400++;
	s_pocsag_scan_last_interval = interval;
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
        "POCSAG preamble diagnostic state",
        flags=re.MULTILINE,
    )

    counters_pattern = (
        r"(?P<counters>"
        r"^[ \t]*if\s*\(\(pd_dinc\s*>\s*421\)\s*&&\s*\(pd_dinc\s*<\s*571\)\)\s*pd_ct24\+\+;\s*\n"
        r"^[ \t]*else\s+if\s*\(pd_ct24\s*>\s*5\)\s*pd_ct24\s*-=\s*3;\s*\n\s*\n"
        r"^[ \t]*if\s*\(\(pd_dinc\s*>\s*842\)\s*&&\s*\(pd_dinc\s*<\s*1142\)\)\s*pd_ct12\+\+;\s*\n"
        r"^[ \t]*else\s+if\s*\(pd_ct12\s*>\s*5\)\s*pd_ct12\s*-=\s*3;\s*\n\s*\n"
        r"^[ \t]*if\s*\(\(pd_dinc\s*>\s*1976\)\s*&&\s*\(pd_dinc\s*<\s*2674\)\)\s*pd_ct5\+\+;\s*\n"
        r"^[ \t]*else\s+if\s*\(pd_ct5\s*>\s*5\)\s*pd_ct5\s*-=\s*3;\s*\n"
        r")"
    )
    decode = sub_once(
        decode,
        counters_pattern,
        lambda match: match.group("counters")
        + "\n\t\t\tpocsag_scan_note(pd_ct5, pd_ct12, pd_ct24, pd_dinc);\n"
        + "\t\t\tpocsag_1200_preamble_note(pd_ct12, pd_dinc);\n",
        "POCSAG preamble counter visibility",
        flags=re.MULTILINE,
    )

    decode_path.write_text(decode, encoding="utf-8")
    print(f"Applied POCSAG scan + 1200 preamble diagnostics to {decode_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
