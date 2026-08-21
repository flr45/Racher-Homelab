#!/usr/bin/env python3
"""Drive the Linux RS232 decoder directly from headless mode.

The GUI path uses a GLib timeout to call pdl_decode(). On the appliance we have
observed the RS232 producer thread receiving data while that timeout never
fires in the no-GUI process. Headless RS232 mode therefore calls pdl_decode()
directly every 20 ms. GUI behavior and the ALSA headless path are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER = "RACHER_HEADLESS_DIRECT_RS232_DECODE"


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
    path = source / "linux" / "main_linux.cpp"
    if not path.is_file():
        raise RuntimeError(f"Missing {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("Headless direct RS232 decode patch already applied")
        return 0

    old = r'''		fprintf(stderr, "[HEADLESS] Ctrl+C/SIGTERM to stop.\n");
		while (!s_headless_stop) {
			/* pdl_linux_hw_decode_start() schedules pdl_decode() through
			 * g_timeout_add(). GUI mode dispatches that source from GTK's
			 * main loop; headless mode must service the default context too. */
			while (g_main_context_iteration(NULL, FALSE))
				;
			usleep(20000);
		}
'''

    new = r'''		fprintf(stderr, "[HEADLESS] Ctrl+C/SIGTERM to stop.\n");
		/* RACHER_HEADLESS_DIRECT_RS232_DECODE: the appliance has no GTK
		 * main loop. The RS232 producer can therefore fill the ring buffer
		 * while a GLib timeout remains undispatched. Consume the ring
		 * directly at the same 20 ms cadence used by the GUI timer. */
		if (Profile.comPortRS232 > 0)
			fprintf(stderr, "[HEADLESS-DECODE] RS232 direct decode loop active; interval_ms=20\n");

		while (!s_headless_stop) {
			if (Profile.comPortRS232 > 0) {
				pdl_decode();
			} else {
				/* Preserve normal GLib dispatch for the ALSA headless path. */
				while (g_main_context_iteration(NULL, FALSE))
					;
			}
			usleep(20000);
		}
'''

    text = replace_once(text, old, new, "headless RS232 decode loop")
    path.write_text(text, encoding="utf-8")
    print(f"Applied direct headless RS232 decode loop to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
