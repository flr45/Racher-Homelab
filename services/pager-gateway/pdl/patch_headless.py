#!/usr/bin/env python3
"""Patch upstream PDL 3.2.0 with a small --headless live-capture mode.

The patch deliberately keeps PDL's decoder and ALSA implementation unchanged.
It only bypasses GTK/WebKit initialization while keeping the existing capture
thread, pdl.ini settings and stdout/log output.
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
    path = source / "linux" / "main_linux.cpp"
    if not path.is_file():
        raise RuntimeError(f"Missing {path}")

    text = path.read_text(encoding="utf-8")
    if "s_headless_stop" in text and '"--headless"' in text:
        print("PDL headless patch already applied")
        return 0

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
        "\t\t\tprintf(\"  --headless          Live ALSA decode without opening a GUI\\n\");\n",
        "headless help",
    )

    marker = "\tint ui = (cli_ui >= 0) ? cli_ui : (Profile.ui_mode == PDL_UI_WEB ? PDL_UI_WEB : PDL_UI_GTK);\n"
    headless_block = r'''	if (headless) {
		signal(SIGINT, headless_signal_handler);
		signal(SIGTERM, headless_signal_handler);

		if (start_capture_thread() != 0) {
			fprintf(stderr, "Failed to open audio capture.\n");
			curl_global_cleanup();
			return 1;
		}

		if (Profile.comPortRS232 > 0)
			pdl_linux_hw_decode_apply_settings();

		fprintf(stderr, "[HEADLESS] PDL live capture active; Ctrl+C/SIGTERM to stop.\n");
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

    path.write_text(text, encoding="utf-8")
    print(f"Applied PDL headless patch to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
