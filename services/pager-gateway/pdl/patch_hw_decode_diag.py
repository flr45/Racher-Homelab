#!/usr/bin/env python3
"""Add privacy-safe telemetry around the Linux RS232 -> pdl_decode() timer.

This patch does not change decoder timing, symbols, polarity or message data.
It only reports whether the GLib timer is running and whether the decoder read
index follows the RS232 producer index.
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER = "RACHER_HW_DECODE_TICK_DIAG"


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
    path = source / "linux" / "hw_decode.cpp"
    if not path.is_file():
        raise RuntimeError(f"Missing {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("Hardware decode tick diagnostics already applied")
        return 0

    state = r'''
/* RACHER_HW_DECODE_TICK_DIAG: metadata-only visibility into the Linux
 * RS232 -> pdl_decode() handoff. No raw symbols, RICs or message text. */
static int s_hw_diag_enabled = -1;
static unsigned long s_hw_diag_ticks = 0;
static gint64 s_hw_diag_last_us = 0;

static int hw_diag_enabled(void)
{
	if (s_hw_diag_enabled < 0) {
		const char *env = getenv("PDL_RS232_RX_DIAG");
		s_hw_diag_enabled = (env && env[0] && env[0] != '0') ? 1 : 0;
	}
	return s_hw_diag_enabled;
}

static unsigned long ring_available(unsigned long producer, unsigned long consumer)
{
	if (bufsize == 0) return 0;
	return (producer >= consumer)
		? (producer - consumer)
		: (bufsize - consumer + producer);
}

static void hw_diag_note(unsigned long cp_before, unsigned int pd_before,
	unsigned long cp_after, unsigned int pd_after)
{
	if (!hw_diag_enabled()) return;
	s_hw_diag_ticks++;

	gint64 now = g_get_monotonic_time();
	if (s_hw_diag_last_us != 0 && now - s_hw_diag_last_us < G_USEC_PER_SEC)
		return;
	s_hw_diag_last_us = now;

	fprintf(stderr,
		"[PDL-TICK] ticks=%lu cp_before=%lu pd_before=%u cp_after=%lu pd_after=%u avail_before=%lu avail_after=%lu paging=%d acars=%d mobitex=%d ermes=%d\n",
		s_hw_diag_ticks,
		cp_before, pd_before, cp_after, pd_after,
		ring_available(cp_before, pd_before),
		ring_available(cp_after, pd_after),
		Profile.monitor_paging ? 1 : 0,
		Profile.monitor_acars ? 1 : 0,
		Profile.monitor_mobitex ? 1 : 0,
		Profile.monitor_ermes ? 1 : 0);
	fflush(stderr);
}
'''

    text = replace_once(
        text,
        "#include <string.h>\n",
        "#include <string.h>\n#include <stdlib.h>\n",
        "stdlib include",
    )
    text = replace_once(
        text,
        "static int s_active = 0;\n",
        "static int s_active = 0;\n" + state + "\n",
        "hardware decode diagnostic state",
    )

    old_callback = '''static gboolean on_hw_decode_tick(gpointer data)\n{\n\t(void)data;\n\tif (!s_active || !cpstn || !freqdata || !linedata) return G_SOURCE_CONTINUE;\n\tpdl_decode();\n\treturn G_SOURCE_CONTINUE;\n}\n'''
    new_callback = '''static gboolean on_hw_decode_tick(gpointer data)\n{\n\t(void)data;\n\tif (!s_active || !cpstn || !freqdata || !linedata) return G_SOURCE_CONTINUE;\n\tunsigned long cp_before = *cpstn;\n\tunsigned int pd_before = pd_i;\n\tpdl_decode();\n\thw_diag_note(cp_before, pd_before, *cpstn, pd_i);\n\treturn G_SOURCE_CONTINUE;\n}\n'''
    text = replace_once(text, old_callback, new_callback, "hardware decode timer callback")

    path.write_text(text, encoding="utf-8")
    print(f"Applied hardware decode tick diagnostics to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
