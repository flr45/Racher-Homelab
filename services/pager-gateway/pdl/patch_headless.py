#!/usr/bin/env python3
"""Patch upstream PDL 3.2.0 for the Racher Pager appliance.

The patch adds a small --headless live-capture mode, stable FSK-USB device
selection and one compatibility correction for Danish POCSAG traffic.

The Linux hardware decoder schedules pdl_decode() with a GLib timeout. GUI
mode normally services that timeout from the GTK main loop, so headless mode
must explicitly pump the default GLib context as well. Without that, the
serial RX thread can fill its ring buffer forever while no pager bits are ever
decoded.

For appliance use the Linux RS232 path also accepts PDL_RS232_DEVICE. This lets
Racher Pager pin the FSK-USB interface to a stable /dev/serial/by-id/... path
instead of relying on ttyUSB enumeration order.

Upstream PDL 3.2.0 additionally forces POCSAG functions 1 and 2 to NUMERIC
before its payload-quality heuristic runs. Function bits do not reliably encode
payload type on every Danish paging network; the previous PDW installation
therefore decoded pages that this shortcut can misclassify. The appliance keeps
function 4 as an alpha hint, but lets functions 1/2 use PDL's normal content
heuristic instead.
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
    pocsag_path = source / "Pocsag.cpp"
    if not main_path.is_file():
        raise RuntimeError(f"Missing {main_path}")
    if not rs232_path.is_file():
        raise RuntimeError(f"Missing {rs232_path}")
    if not pocsag_path.is_file():
        raise RuntimeError(f"Missing {pocsag_path}")

    text = main_path.read_text(encoding="utf-8")
    rs232_text = rs232_path.read_text(encoding="utf-8")
    pocsag_text = pocsag_path.read_text(encoding="utf-8")
    headless_done = "s_headless_stop" in text and '"--headless"' in text
    rs232_device_done = 'getenv("PDL_RS232_DEVICE")' in rs232_text
    pocsag_payload_done = "RACHER_POCSAG_PAYLOAD_HEURISTIC" in pocsag_text

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
		while (!s_headless_stop) {
			/* pdl_linux_hw_decode_start() schedules pdl_decode() through
			 * g_timeout_add(). GUI mode dispatches that source from GTK's
			 * main loop; headless mode must service the default context too. */
			while (g_main_context_iteration(NULL, FALSE))
				;
			usleep(20000);
		}

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

    if not pocsag_payload_done:
        pocsag_text = replace_once(
            pocsag_text,
            "\tif (function == 1 || function == 2)\n"
            "\t\treturn(TYPE_NUMERIC);\n",
            "\t/* RACHER_POCSAG_PAYLOAD_HEURISTIC: function 1/2 is not a reliable\n"
            "\t * numeric/alpha discriminator on the Danish paging networks we\n"
            "\t * receive. Let the existing content-quality heuristic decide. */\n",
            "POCSAG function 1/2 payload heuristic",
        )
        pocsag_path.write_text(pocsag_text, encoding="utf-8")
        print(f"Applied POCSAG payload heuristic patch to {pocsag_path}")
    else:
        print("POCSAG payload heuristic patch already applied")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
