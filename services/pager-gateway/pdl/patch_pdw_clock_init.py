#!/usr/bin/env python3
"""Restore the legacy PDW decoder clock initialization on Linux.

The original Discriminator/PDW startup initializes ct1600, ct3200 and ct_bit
before the serial decoder starts. The Linux port declares and uses the same
globals, but its Linux startup path does not initialize them explicitly.

With RS232 DecodeMode=1 an initial ct_bit of zero can make the inner decoder
clock loop non-progressing because it subtracts ct_bit on each iteration and
the mode-1 path has no >1 escape. This patch mirrors the original PDW startup
constants and state without changing POCSAG thresholds, serial framing, baud
selection, polarity or payload handling.
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER = "RACHER_PDW_CLOCK_INIT"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Could not patch {label}: expected 1 match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <PDL source directory>", file=sys.stderr)
        return 2

    source = Path(sys.argv[1]).resolve()
    path = source / "linux" / "init_linux.cpp"
    if not path.is_file():
        raise RuntimeError(f"Missing {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("Legacy PDW clock initialization already applied")
        return 0

    if '#include "Headers/decode.h"' not in text:
        text = replace_once(
            text,
            '#include "Headers/sound_in.h"\n',
            '#include "Headers/sound_in.h"\n#include "Headers/decode.h"\n',
            "decode header include",
        )

    anchor = "\tSetAudioConfig(Profile.audioConfig > 0 ? Profile.audioConfig : 1);\n"
    block = r'''	/* RACHER_PDW_CLOCK_INIT: mirror original Discriminator/PDW startup.
	 * These are hardware-timer ticks per 1600/3200 baud bit.  The legacy
	 * serial decoder expects ct_bit to be positive before its first call. */
	for (int i = 0; i < 64; i++) rcver[i] = 0.0;
	rcv_clkt = rcv_clkt_hi;
	ct1600 = 1.0 / (1600.0 * 838.8e-9); /* 745.1 */
	ct3200 = 1.0 / (3200.0 * 838.8e-9); /* 372.5 */
	ct_bit = ct1600;
	fprintf(stderr,
		"[PDW-CLOCK] legacy timing initialized; ct1600=%.1f ct3200=%.1f ct_bit=%.1f\n",
		ct1600, ct3200, ct_bit);
'''

    text = replace_once(text, anchor, block + anchor, "legacy PDW clock initialization")
    path.write_text(text, encoding="utf-8")
    print(f"Applied legacy PDW clock initialization to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
