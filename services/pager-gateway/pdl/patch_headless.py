#!/usr/bin/env python3
"""Patch upstream PDL 3.2.0 for the Racher Pager appliance.

The patch adds a small --headless live-capture mode, stable FSK-USB device
selection, privacy-safe raw FSK receive diagnostics and one compatibility
correction for Danish POCSAG traffic.

The Linux hardware decoder schedules pdl_decode() with a GLib timeout. GUI
mode normally services that timeout from the GTK main loop, so headless mode
must explicitly pump the default GLib context as well. Without that, the
serial RX thread can fill its ring buffer forever while no pager bits are ever
decoded.

For appliance use the Linux RS232 path also accepts PDL_RS232_DEVICE. This lets
Racher Pager pin the FSK-USB interface to a stable /dev/serial/by-id/... path
instead of relying on ttyUSB enumeration order.

The RS232 path can additionally emit one [FSK-RX] summary per receive burst
when PDL_RS232_RX_DIAG=1. The summary contains only byte/symbol counts and
transition statistics, never raw pager bytes, capcodes or message text. This
lets us distinguish "the FTDI delivered a burst but PDL could not sync" from
"the FTDI delivered no data" when a known alarm is missed.

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
    rs232_diag_done = "RACHER_FSK_RX_DIAG" in rs232_text
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

    if not rs232_diag_done:
        rs232_text = replace_once(
            rs232_text,
            "static int s_rx_thread_running = 0;\n",
            "static int s_rx_thread_running = 0;\n\n"
            "/* RACHER_FSK_RX_DIAG: privacy-safe metadata for missed-page diagnosis. */\n"
            "static int s_rx_diag_enabled = 0;\n"
            "static unsigned long s_rx_diag_symbols = 0;\n"
            "static unsigned long s_rx_diag_nonzero = 0;\n"
            "static unsigned long s_rx_diag_transitions = 0;\n"
            "static int s_rx_diag_last_symbol = -1;\n\n"
            "static void rx_diag_reset_symbols(void)\n"
            "{\n"
            "\ts_rx_diag_symbols = 0;\n"
            "\ts_rx_diag_nonzero = 0;\n"
            "\ts_rx_diag_transitions = 0;\n"
            "\ts_rx_diag_last_symbol = -1;\n"
            "}\n\n"
            "static void rx_diag_note_symbol(int symbol)\n"
            "{\n"
            "\tif (!s_rx_diag_enabled) return;\n"
            "\ts_rx_diag_symbols++;\n"
            "\tif (symbol != 0) s_rx_diag_nonzero++;\n"
            "\tif (s_rx_diag_last_symbol >= 0 && symbol != s_rx_diag_last_symbol)\n"
            "\t\ts_rx_diag_transitions++;\n"
            "\ts_rx_diag_last_symbol = symbol;\n"
            "}\n\n"
            "static void rx_diag_flush(unsigned long bytes, unsigned long reads)\n"
            "{\n"
            "\tif (!s_rx_diag_enabled || bytes == 0) return;\n"
            "\tfprintf(stderr,\n"
            "\t\t\"[FSK-RX] burst bytes=%lu reads=%lu symbols=%lu nonzero=%lu transitions=%lu\\n\",\n"
            "\t\tbytes, reads, s_rx_diag_symbols, s_rx_diag_nonzero, s_rx_diag_transitions);\n"
            "\tfflush(stderr);\n"
            "}\n",
            "FSK RX diagnostic state",
        )

        rs232_text = replace_once(
            rs232_text,
            "\t\t\ts_linedata[s_cpstn] = (unsigned char)(bit << 4);\n",
            "\t\t\trx_diag_note_symbol(bit);\n"
            "\t\t\ts_linedata[s_cpstn] = (unsigned char)(bit << 4);\n",
            "FSK RX diagnostic symbol count",
        )

        rs232_text = replace_once(
            rs232_text,
            "static void *rx_thread_fn(void *arg)\n"
            "{\n"
            "\t(void)arg;\n"
            "\twhile (s_rx_alive) {\n"
            "\t\tif (s_fd_in < 0) break;\n"
            "\t\tfd_set rfds;\n"
            "\t\tFD_ZERO(&rfds);\n"
            "\t\tFD_SET(s_fd_in, &rfds);\n"
            "\t\tstruct timeval tv;\n"
            "\t\ttv.tv_sec = 0;\n"
            "\t\ttv.tv_usec = 50000;\n"
            "\t\tint r = select(s_fd_in + 1, &rfds, NULL, NULL, &tv);\n"
            "\t\tif (r > 0 && FD_ISSET(s_fd_in, &rfds))\n"
            "\t\t\trs232_read();\n"
            "\t}\n"
            "\treturn NULL;\n"
            "}\n",
            "static void *rx_thread_fn(void *arg)\n"
            "{\n"
            "\t(void)arg;\n"
            "\tunsigned long burst_bytes = 0;\n"
            "\tunsigned long burst_reads = 0;\n"
            "\tint idle_windows = 0;\n\n"
            "\twhile (s_rx_alive) {\n"
            "\t\tif (s_fd_in < 0) break;\n"
            "\t\tfd_set rfds;\n"
            "\t\tFD_ZERO(&rfds);\n"
            "\t\tFD_SET(s_fd_in, &rfds);\n"
            "\t\tstruct timeval tv;\n"
            "\t\ttv.tv_sec = 0;\n"
            "\t\ttv.tv_usec = 50000;\n"
            "\t\tint r = select(s_fd_in + 1, &rfds, NULL, NULL, &tv);\n"
            "\t\tif (r > 0 && FD_ISSET(s_fd_in, &rfds)) {\n"
            "\t\t\tint nread = rs232_read();\n"
            "\t\t\tif (nread > 0) {\n"
            "\t\t\t\tif (burst_bytes == 0) rx_diag_reset_symbols();\n"
            "\t\t\t\tburst_bytes += (unsigned long)nread;\n"
            "\t\t\t\tburst_reads++;\n"
            "\t\t\t\tidle_windows = 0;\n"
            "\t\t\t\tcontinue;\n"
            "\t\t\t}\n"
            "\t\t}\n\n"
            "\t\t/* Four 50 ms quiet select windows delimit one hardware burst. */\n"
            "\t\tif (s_rx_diag_enabled && burst_bytes > 0 && ++idle_windows >= 4) {\n"
            "\t\t\trx_diag_flush(burst_bytes, burst_reads);\n"
            "\t\t\tburst_bytes = 0;\n"
            "\t\t\tburst_reads = 0;\n"
            "\t\t\tidle_windows = 0;\n"
            "\t\t\trx_diag_reset_symbols();\n"
            "\t\t}\n"
            "\t}\n\n"
            "\tif (s_rx_diag_enabled && burst_bytes > 0)\n"
            "\t\trx_diag_flush(burst_bytes, burst_reads);\n"
            "\treturn NULL;\n"
            "}\n",
            "FSK RX diagnostic burst summaries",
        )

        rs232_text = replace_once(
            rs232_text,
            "\tmemset(s_freqdata, 0, sizeof(s_freqdata));\n"
            "\tmemset(s_linedata, 0, sizeof(s_linedata));\n"
            "\ts_cpstn = 0;\n\n"
            "\ts_rx_alive = 1;\n",
            "\tmemset(s_freqdata, 0, sizeof(s_freqdata));\n"
            "\tmemset(s_linedata, 0, sizeof(s_linedata));\n"
            "\ts_cpstn = 0;\n\n"
            "\tconst char *diag_env = getenv(\"PDL_RS232_RX_DIAG\");\n"
            "\ts_rx_diag_enabled = (diag_env && diag_env[0] && strcmp(diag_env, \"0\") != 0);\n"
            "\trx_diag_reset_symbols();\n"
            "\tif (s_rx_diag_enabled) {\n"
            "\t\tfprintf(stderr, \"[FSK-RX] diagnostics active; burst_gap_ms=200 metadata_only=1\\n\");\n"
            "\t\tfflush(stderr);\n"
            "\t}\n\n"
            "\ts_rx_alive = 1;\n",
            "FSK RX diagnostic enable",
        )

        rs232_path.write_text(rs232_text, encoding="utf-8")
        print(f"Applied privacy-safe FSK RX diagnostics to {rs232_path}")
    else:
        print("FSK RX diagnostics already applied")

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
