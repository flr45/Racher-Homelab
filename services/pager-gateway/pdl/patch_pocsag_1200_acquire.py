#!/usr/bin/env python3
"""Make the POCSAG 1200 preamble acquisition threshold runtime configurable.

The original PDW/PDL decoder waits until pd_ct12 > 180 before it attempts
POCSAG-1200 sync. Live FSK-USB telemetry from a missed Vordingborg dispatch
showed a clean 1200 preamble reaching 166, then falling away before the legacy
threshold. Background traffic was far lower.

This patch keeps the real POCSAG sync word and BCH validation unchanged. It only
allows the decoder to *attempt* sync earlier. The appliance default is 120 and
can be overridden with PDL_POCSAG_1200_ACQUIRE_THRESHOLD (clamped 60..180).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


MARKER = "RACHER_POCSAG_1200_ACQUIRE_THRESHOLD"
DEFAULT_THRESHOLD = 120


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
        print("POCSAG-1200 acquisition threshold patch already applied")
        return 0

    if "RACHER_POCSAG_1200_PREAMBLE_DIAG" not in decode:
        raise RuntimeError(
            "patch_pocsag_1200_acquire.py must run after patch_pocsag_1200_preamble.py"
        )

    helper = r'''
/* RACHER_POCSAG_1200_ACQUIRE_THRESHOLD: FSK-USB live reception showed a
 * genuine 1200-baud preamble reaching 166 without crossing legacy >180.
 * Lowering acquisition only starts sync search earlier; POCSAG sync/BCH checks
 * remain authoritative, so background noise is not promoted directly to pages. */
static int racher_pocsag_1200_acquire_threshold(void)
{
	static int cached = -1;
	if (cached >= 0) return cached;

	cached = 120;
	const char *env = getenv("PDL_POCSAG_1200_ACQUIRE_THRESHOLD");
	if (env && env[0]) {
		char *end = NULL;
		long value = strtol(env, &end, 10);
		if (end != env && *end == '\0') {
			if (value < 60) value = 60;
			if (value > 180) value = 180;
			cached = (int)value;
		}
	}
	return cached;
}
'''

    # Use a callback replacement so C/C++ backslash escapes in helper (notably
    # '\0') are copied byte-for-byte instead of being interpreted by re.sub.
    decode = sub_once(
        decode,
        r"(?m)^(static int s_pocsag_1200_preamble_enabled = -1;)$",
        lambda match: helper + "\n" + match.group(1),
        "1200 acquisition helper",
    )

    decode = sub_once(
        decode,
        r"if \(count > 180\)",
        "if (count > racher_pocsag_1200_acquire_threshold())",
        "1200 diagnostic acquisition threshold",
    )

    decode = sub_once(
        decode,
        r"else if \(pd_ct12 > 180\)",
        "else if (pd_ct12 > racher_pocsag_1200_acquire_threshold())",
        "POCSAG-1200 decoder acquisition threshold",
    )

    decode_path.write_text(decode, encoding="utf-8")
    print(
        f"Applied runtime POCSAG-1200 acquisition threshold to {decode_path} "
        f"(default={DEFAULT_THRESHOLD}, env=PDL_POCSAG_1200_ACQUIRE_THRESHOLD)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
