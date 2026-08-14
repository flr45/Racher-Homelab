#!/usr/bin/env python3
"""Patch upstream PDL 3.2.0 with a small --headless live-capture mode.

The patch deliberately keeps PDL's decoder, Linux RS232 bitstream input and
ALSA implementation unchanged. It only bypasses GTK/WebKit initialization so
headless mode can run either an FSK->USB/RS232 converter or ALSA capture while
keeping pdl.ini settings and stdout/log output.

For appliance use the Linux RS232 path also accepts PDL_RS232_DEVICE. This lets
Racher Pager pin the FSK-USB interface to a stable /dev/serial/by-id/... path
instead of relying on ttyUSB enumeration order.
"""
from __future__ import annotations

import sys
from pathlib import Path


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
    main_path = source / "linux" / "main_linux.cpp"
    rs232_path = source / "linux" / "rs232_linux.cpp"
    if not main_path.is_file():
        raise RuntimeError(f"Missing {main_path}")
    if not rs232_path.is_file():
        raise RuntimeError(f"Missing {rs232_path}")

    text = main_path.read_text(encoding="utf-8")
    rs232_text = rs232_path.read_text(encoding="utf-8")
    headless_done = "s_headless_stop" in text and '"--headless"' in text
    rs232_device_done = 'getenv("PDL_RS232_DEVICE")' in rs232_text

    if not headless_done:
        text = replace_once(
            text,
            "#include <stdint.h>\n",
            "#include <stdint.h>\n#include <signal.h>\n",
            "signal include",
        )

        text = replace_once(
            text,
            "static char **s_saved_argv;\n",
            "static char **s_saved_argv;\n"
            "static volatile sig_atomic_t s_headless_stop = 0;\n\n"
            "static void headless_signal_handler(int signo)\n"
            "{\n"
            "\t(void)signo;\n"
            "\ts_headless_stop = 1;\n"
            "}\n",
            "headless signal state",
        )

        text = replace_once(
            text,
            "\tint force_invert = -1; /* -1=ini/auto, 0=off, 1=on */\n",
            "\tint force_invert = -1; /* -1=ini/auto, 0=off, 1=on */\n"
            "\tint headless = 0;\n",
            "headless flag",
        )

        text = replace_once(
            text,
            "\t\t} else if (strcmp(argv[i], \"--no-invert\") == 0) {\n"
            "\t\t\tforce_invert = 0;\n"
            "\t\t} else if (strcmp(argv[i], \"-h\") == 0 || strcmp(argv[i], \"--help\") == 0) {\n",
            "\t\t} else if (strcmp(argv[i], \"--no-invert\") == 0) {\n"
            "\t\t\tforce_invert = 0;\n"
            "\t\t} else if (strcmp(argv[i], \"--headless\") == 0) {\n"
            "\t\t\theadless = 1;\n"
            "\t\t} else if (strcmp(argv[i], \"-h\") == 0 || strcmp(argv[i], \"--help\") == 0) {\n",
            "headless argument",
        )

        text = replace_once(
            text,
            "\t\t\tprintf(\"  --no-invert         Force normal polarity\\n\");\n",
            "\t\t\tprintf(\"  --no-invert         Force normal polarity\\n\");\n"
            "\t\t\tprintf(\"  --headless          Live RS232/ALSA decode without opening a GUI\\n\");\n",
            "headless help",
        )

        marker = "\tint ui = (cli_ui >= 0) ? cli_ui : (Profile.ui_mode == PDL_UI_WEB ? PDL_UI_WEB : PDL_UI_GTK);\n"
        headless_block = r'''	if (headless) {
		signal(SIGINT, headless_signal_handler);
		signal(SIGTERM, headless_signal_handler);

		if (Profile.comPortRS232 > 0) {
			/* The FSK->USB converter already performs slicing/bit timing.  Do
			 * not require or open ALSA before starting the existing Linux
			 * RS232 bitstream decoder. */
			if (pdl_linux_hw_decode_start() != 0) {
				fprintf(stderr, "Failed to open RS232/FSK bitstream input.\n");
				curl_global_cleanup();
				return 1;
			}
			fprintf(stderr, "[HEADLESS] PDL RS232/FSK bitstream decode active.\n");
		} else {
			if (start_capture_thread() != 0) {
				fprintf(stderr, "Failed to open audio capture.\n");
				curl_global_cleanup();
				return 1;
			}
			fprintf(stderr, "[HEADLESS] PDL ALSA live capture active.\n");
		}

		fprintf(stderr, "[HEADLESS] Ctrl+C/SIGTERM to stop.\n");
		while (!s_headless_stop)
			sleep(1);

		pdl_linux_hw_decode_stop();
		if (s_capture_active) {
			pdl_linux_alsa_stop();
			pthread_join(s_capture_thread, NULL);
			pdl_linux_alsa_close();
			s_capture_active = 0;
		}
		curl_global_cleanup();
		return 0;
	}

'''
        text = replace_once(text, marker, headless_block + marker, "headless runtime")
        main_path.write_text(text, encoding="utf-8")
        print(f"Applied PDL headless patch to {main_path}")
    else:
        print("PDL headless patch already applied")

    if not rs232_device_done:
        rs232_text = replace_once(
            rs232_text,
            "\tconst char *path = port_path_for_index(port);\n",
            "\tconst char *env_path = getenv(\"PDL_RS232_DEVICE\");\n"
            "\tconst char *path = (env_path && env_path[0]) ? env_path : port_path_for_index(port);\n",
            "explicit RS232 device",
        )
        rs232_path.write_text(rs232_text, encoding="utf-8")
        print(f"Applied PDL explicit RS232-device patch to {rs232_path}")
    else:
        print("PDL explicit RS232-device patch already applied")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
