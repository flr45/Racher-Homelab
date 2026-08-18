#!/usr/bin/env python3
"""Add privacy-safe POCSAG-512 post-sync diagnostics to pinned PDL.

The diagnostic is enabled with PDL_POCSAG_512_DIAG=1 and emits one summary
per decoded 512-baud batch. It deliberately logs only decoder metadata:
sync distance, codeword counts and BCH correction buckets. It never logs
capcodes/RICs, message bits or message text.
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER = "RACHER_POCSAG_512_DIAG"


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
    pocsag_path = source / "Pocsag.cpp"
    if not pocsag_path.is_file():
        raise RuntimeError(f"Missing {pocsag_path}")

    text = pocsag_path.read_text(encoding="utf-8")
    if MARKER in text:
        print("POCSAG-512 diagnostics already applied")
        return 0

    text = replace_once(
        text,
        "#include <stdio.h>\n",
        "#include <stdio.h>\n#include <stdlib.h>\n",
        "stdlib include",
    )

    diag_state = r'''
/* RACHER_POCSAG_512_DIAG: privacy-safe post-sync decoder diagnostics.
 * Never log capcodes, message bits or message text here. */
static int s_pocsag_diag_enabled = -1;
static int s_pocsag_diag_batch_active = 0;
static int s_pocsag_diag_sync_errors = 0;
static int s_pocsag_diag_inverted = 0;
static unsigned long s_pocsag_diag_words = 0;
static unsigned long s_pocsag_diag_address_words = 0;
static unsigned long s_pocsag_diag_message_words = 0;
static unsigned long s_pocsag_diag_bch0 = 0;
static unsigned long s_pocsag_diag_bch1 = 0;
static unsigned long s_pocsag_diag_bch2 = 0;
static unsigned long s_pocsag_diag_bch_gt2 = 0;

static int pocsag_512_diag_enabled(void)
{
	if (s_pocsag_diag_enabled < 0) {
		const char *env = getenv("PDL_POCSAG_512_DIAG");
		s_pocsag_diag_enabled = (env && env[0] && strcmp(env, "0") != 0) ? 1 : 0;
	}
	return s_pocsag_diag_enabled;
}

static void pocsag_512_diag_begin(int sync_errors, int inverted)
{
	if (!pocsag_512_diag_enabled() || pocsag_baud_rate != STAT_POCSAG512) {
		s_pocsag_diag_batch_active = 0;
		return;
	}
	s_pocsag_diag_batch_active = 1;
	s_pocsag_diag_sync_errors = sync_errors;
	s_pocsag_diag_inverted = inverted;
	s_pocsag_diag_words = 0;
	s_pocsag_diag_address_words = 0;
	s_pocsag_diag_message_words = 0;
	s_pocsag_diag_bch0 = 0;
	s_pocsag_diag_bch1 = 0;
	s_pocsag_diag_bch2 = 0;
	s_pocsag_diag_bch_gt2 = 0;
}

static void pocsag_512_diag_note_word(int errl, int is_message)
{
	if (!s_pocsag_diag_batch_active) return;
	s_pocsag_diag_words++;
	if (is_message) s_pocsag_diag_message_words++;
	else s_pocsag_diag_address_words++;

	if (errl <= 0) s_pocsag_diag_bch0++;
	else if (errl == 1) s_pocsag_diag_bch1++;
	else if (errl == 2) s_pocsag_diag_bch2++;
	else s_pocsag_diag_bch_gt2++;
}

static void pocsag_512_diag_flush(const char *reason)
{
	if (!s_pocsag_diag_batch_active) return;
	fprintf(stderr,
		"[POCSAG-DIAG] baud=512 sync_errors=%d inverted=%d words=%lu address=%lu message=%lu bch0=%lu bch1=%lu bch2=%lu bch_gt2=%lu reason=%s\n",
		s_pocsag_diag_sync_errors, s_pocsag_diag_inverted,
		s_pocsag_diag_words, s_pocsag_diag_address_words, s_pocsag_diag_message_words,
		s_pocsag_diag_bch0, s_pocsag_diag_bch1, s_pocsag_diag_bch2,
		s_pocsag_diag_bch_gt2, reason ? reason : "unknown");
	fflush(stderr);
	s_pocsag_diag_batch_active = 0;
}
'''

    text = replace_once(
        text,
        "extern int pocsag_baud_rate, pocbit;\n",
        "extern int pocsag_baud_rate, pocbit;\n" + diag_state,
        "POCSAG diagnostic state",
    )

    text = replace_once(
        text,
        "\telse if (bit == -1)\t// reset (leave POCSAG / end of burst)\n\t{\n\t\tbSynced = false;\n",
        "\telse if (bit == -1)\t// reset (leave POCSAG / end of burst)\n\t{\n\t\tpocsag_512_diag_flush(\"decoder-reset\");\n\t\tbSynced = false;\n",
        "POCSAG reset diagnostic flush",
    )

    text = replace_once(
        text,
        "\t\tif (nh < 5)\n\t\t{\n\t\t\tbSynced = true;\n\t\t\tiWordNumber = 0;\n\t\t\tcc = 0;\n\t\t\tpocsag_keep_alive();\n\t\t}\n",
        "\t\tif (nh < 5)\n\t\t{\n\t\t\tbSynced = true;\n\t\t\tiWordNumber = 0;\n\t\t\tcc = 0;\n\t\t\tpocsag_512_diag_begin(nh, 0);\n\t\t\tpocsag_keep_alive();\n\t\t}\n",
        "normal POCSAG sync diagnostic",
    )

    text = replace_once(
        text,
        "\t\t\tbSynced = true;\n\t\t\tiWordNumber = 0;\n\t\t\tcc = 0;\n\t\t\tpocsag_keep_alive();\n\t\t}\n\t}\n\telse\t// format, process 16 by 32 bit paging block\n",
        "\t\t\tbSynced = true;\n\t\t\tiWordNumber = 0;\n\t\t\tcc = 0;\n\t\t\tpocsag_512_diag_begin(0, 1);\n\t\t\tpocsag_keep_alive();\n\t\t}\n\t}\n\telse\t// format, process 16 by 32 bit paging block\n",
        "inverted POCSAG sync diagnostic",
    )

    text = replace_once(
        text,
        "\t\tif (iWordNumber == POCSAG_WORDS_PER_BATCH)\n\t\t{\n\t\t\tbSynced = false;\t// if block count is zero go back to look for sync word\n\t\t}\n",
        "\t\tif (iWordNumber == POCSAG_WORDS_PER_BATCH)\n\t\t{\n\t\t\tpocsag_512_diag_flush(\"batch-end\");\n\t\t\tbSynced = false;\t// if block count is zero go back to look for sync word\n\t\t}\n",
        "POCSAG batch diagnostic flush",
    )

    text = replace_once(
        text,
        "\tint i, errl = ecd();\t\t// run error correcting routine\n\n\t/* Extend hold-off on every codeword so noisy stretches don't drop the page. */\n",
        "\tint i, errl = ecd();\t\t// run error correcting routine\n\tpocsag_512_diag_note_word(errl, ob[MSB] == 1);\n\n\t/* Extend hold-off on every codeword so noisy stretches don't drop the page. */\n",
        "POCSAG BCH diagnostic count",
    )

    pocsag_path.write_text(text, encoding="utf-8")
    print(f"Applied POCSAG-512 BCH diagnostics to {pocsag_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
