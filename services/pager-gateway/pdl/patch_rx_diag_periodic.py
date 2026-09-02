#!/usr/bin/env python3
"""Convert Racher FSK diagnostics from idle-gap bursts to fixed windows.

The discriminator.nl FSK-to-USB interface can stream continuously, so an
idle-gap-only diagnostic may never flush even while PDL is decoding pages.
Use one-second metadata-only windows instead. No raw bytes, RICs or message
text are logged.
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER = "RACHER_FSK_RX_PERIODIC"


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
    rs232_path = source / "linux" / "rs232_linux.cpp"
    if not rs232_path.is_file():
        raise RuntimeError(f"Missing {rs232_path}")

    text = rs232_path.read_text(encoding="utf-8")
    if MARKER in text:
        print("Periodic FSK RX diagnostics already applied")
        return 0

    text = replace_once(
        text,
        "#include <sys/select.h>\n",
        "#include <sys/select.h>\n#include <time.h>\n",
        "monotonic clock include",
    )

    text = replace_once(
        text,
        '"[FSK-RX] burst bytes=%lu reads=%lu symbols=%lu nonzero=%lu transitions=%lu\\n",',
        '"[FSK-RX] window bytes=%lu reads=%lu symbols=%lu nonzero=%lu transitions=%lu\\n",',
        "FSK diagnostic window label",
    )

    old_thread = '''static void *rx_thread_fn(void *arg)
{
\t(void)arg;
\tunsigned long burst_bytes = 0;
\tunsigned long burst_reads = 0;
\tint idle_windows = 0;

\twhile (s_rx_alive) {
\t\tif (s_fd_in < 0) break;
\t\tfd_set rfds;
\t\tFD_ZERO(&rfds);
\t\tFD_SET(s_fd_in, &rfds);
\t\tstruct timeval tv;
\t\ttv.tv_sec = 0;
\t\ttv.tv_usec = 50000;
\t\tint r = select(s_fd_in + 1, &rfds, NULL, NULL, &tv);
\t\tif (r > 0 && FD_ISSET(s_fd_in, &rfds)) {
\t\t\tint nread = rs232_read();
\t\t\tif (nread > 0) {
\t\t\t\tif (burst_bytes == 0) rx_diag_reset_symbols();
\t\t\t\tburst_bytes += (unsigned long)nread;
\t\t\t\tburst_reads++;
\t\t\t\tidle_windows = 0;
\t\t\t\tcontinue;
\t\t\t}
\t\t}

\t\t/* Four 50 ms quiet select windows delimit one hardware burst. */
\t\tif (s_rx_diag_enabled && burst_bytes > 0 && ++idle_windows >= 4) {
\t\t\trx_diag_flush(burst_bytes, burst_reads);
\t\t\tburst_bytes = 0;
\t\t\tburst_reads = 0;
\t\t\tidle_windows = 0;
\t\t\trx_diag_reset_symbols();
\t\t}
\t}

\tif (s_rx_diag_enabled && burst_bytes > 0)
\t\trx_diag_flush(burst_bytes, burst_reads);
\treturn NULL;
}
'''

    new_thread = '''static void *rx_thread_fn(void *arg)
{
\t(void)arg;
\t/* RACHER_FSK_RX_PERIODIC: the FSK-to-USB adapter may stream without an
\t * idle gap, so emit one privacy-safe receive summary per second. */
\tunsigned long window_bytes = 0;
\tunsigned long window_reads = 0;
\tstruct timespec window_started;
\tclock_gettime(CLOCK_MONOTONIC, &window_started);
\trx_diag_reset_symbols();

\twhile (s_rx_alive) {
\t\tif (s_fd_in < 0) break;
\t\tfd_set rfds;
\t\tFD_ZERO(&rfds);
\t\tFD_SET(s_fd_in, &rfds);
\t\tstruct timeval tv;
\t\ttv.tv_sec = 0;
\t\ttv.tv_usec = 50000;
\t\tint r = select(s_fd_in + 1, &rfds, NULL, NULL, &tv);
\t\tif (r > 0 && FD_ISSET(s_fd_in, &rfds)) {
\t\t\tint nread = rs232_read();
\t\t\tif (nread > 0) {
\t\t\t\twindow_bytes += (unsigned long)nread;
\t\t\t\twindow_reads++;
\t\t\t}
\t\t}

\t\tif (s_rx_diag_enabled) {
\t\t\tstruct timespec now;
\t\t\tclock_gettime(CLOCK_MONOTONIC, &now);
\t\t\tlong elapsed_ms = (long)(now.tv_sec - window_started.tv_sec) * 1000L
\t\t\t\t+ (long)(now.tv_nsec - window_started.tv_nsec) / 1000000L;
\t\t\tif (elapsed_ms >= 1000L) {
\t\t\t\trx_diag_flush(window_bytes, window_reads);
\t\t\t\twindow_bytes = 0;
\t\t\t\twindow_reads = 0;
\t\t\t\trx_diag_reset_symbols();
\t\t\t\twindow_started = now;
\t\t\t}
\t\t}
\t}

\tif (s_rx_diag_enabled && window_bytes > 0)
\t\trx_diag_flush(window_bytes, window_reads);
\treturn NULL;
}
'''

    text = replace_once(text, old_thread, new_thread, "continuous FSK receive diagnostics")
    text = replace_once(
        text,
        '"[FSK-RX] diagnostics active; burst_gap_ms=200 metadata_only=1\\n"',
        '"[FSK-RX] diagnostics active; window_ms=1000 metadata_only=1\\n"',
        "FSK diagnostic startup label",
    )

    rs232_path.write_text(text, encoding="utf-8")
    print(f"Applied periodic FSK RX diagnostics to {rs232_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
