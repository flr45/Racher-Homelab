#!/usr/bin/env python3
"""Add privacy-safe POCSAG-1200 preamble/sync/BCH diagnostics to pinned PDL.

This patch is intentionally diagnostic-only. It does not change decoder
thresholds, polarity, timing or error-correction decisions.

PDL_POCSAG_1200_DIAG=1 enables:
- [POCSAG-PREAMBLE] checkpoints while the 1200-baud preamble counter climbs.
- [POCSAG-PRESYNC] Hamming-distance summaries while looking for frame sync.
- [POCSAG-DIAG] BCH/codeword quality after frame sync.

No capcodes/RICs, message text, raw bits or raw serial bytes are logged.
The existing POCSAG-512 diagnostic patch must be applied first.
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER = "RACHER_POCSAG_1200_DIAG"
PREAMBLE_MARKER = "RACHER_POCSAG_1200_PREAMBLE_DIAG"


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
    decode_path = source / "decode.cpp"
    if not pocsag_path.is_file():
        raise RuntimeError(f"Missing {pocsag_path}")
    if not decode_path.is_file():
        raise RuntimeError(f"Missing {decode_path}")

    pocsag = pocsag_path.read_text(encoding="utf-8")
    decode = decode_path.read_text(encoding="utf-8")

    if "RACHER_POCSAG_512_DIAG" not in pocsag:
        raise RuntimeError("POCSAG-512 diagnostic patch must be applied before the 1200 patch")

    if MARKER not in pocsag:
        diag_state = r'''
/* RACHER_POCSAG_1200_DIAG: privacy-safe 1200-baud frame diagnostics.
 * Never log capcodes, message bits, raw sync bits or message text here. */
static int s_pocsag_1200_diag_enabled = -1;

static int s_pocsag_1200_presync_active = 0;
static unsigned long s_pocsag_1200_presync_samples = 0;
static int s_pocsag_1200_presync_best_normal = 33;
static int s_pocsag_1200_presync_best_inverted = 33;
static unsigned long s_pocsag_1200_presync_normal5 = 0;
static unsigned long s_pocsag_1200_presync_normal6 = 0;
static unsigned long s_pocsag_1200_presync_normal7 = 0;
static unsigned long s_pocsag_1200_presync_inverted1to4 = 0;
static unsigned long s_pocsag_1200_presync_inverted5 = 0;
static unsigned long s_pocsag_1200_presync_inverted6 = 0;
static unsigned long s_pocsag_1200_presync_inverted7 = 0;

static int s_pocsag_1200_batch_active = 0;
static int s_pocsag_1200_sync_errors = 0;
static int s_pocsag_1200_inverted = 0;
static unsigned long s_pocsag_1200_words = 0;
static unsigned long s_pocsag_1200_address_words = 0;
static unsigned long s_pocsag_1200_message_words = 0;
static unsigned long s_pocsag_1200_bch0 = 0;
static unsigned long s_pocsag_1200_bch1 = 0;
static unsigned long s_pocsag_1200_bch2 = 0;
static unsigned long s_pocsag_1200_bch_gt2 = 0;

static int pocsag_1200_diag_enabled(void)
{
	if (s_pocsag_1200_diag_enabled < 0) {
		const char *env = getenv("PDL_POCSAG_1200_DIAG");
		s_pocsag_1200_diag_enabled = (env && env[0] && strcmp(env, "0") != 0) ? 1 : 0;
	}
	return s_pocsag_1200_diag_enabled;
}

static void pocsag_1200_presync_begin(void)
{
	if (!pocsag_1200_diag_enabled() || pocsag_baud_rate != STAT_POCSAG1200) return;
	if (s_pocsag_1200_presync_active) return;

	s_pocsag_1200_presync_active = 1;
	s_pocsag_1200_presync_samples = 0;
	s_pocsag_1200_presync_best_normal = 33;
	s_pocsag_1200_presync_best_inverted = 33;
	s_pocsag_1200_presync_normal5 = 0;
	s_pocsag_1200_presync_normal6 = 0;
	s_pocsag_1200_presync_normal7 = 0;
	s_pocsag_1200_presync_inverted1to4 = 0;
	s_pocsag_1200_presync_inverted5 = 0;
	s_pocsag_1200_presync_inverted6 = 0;
	s_pocsag_1200_presync_inverted7 = 0;
}

static void pocsag_1200_presync_note(int normal_distance)
{
	if (!pocsag_1200_diag_enabled() || pocsag_baud_rate != STAT_POCSAG1200) return;
	pocsag_1200_presync_begin();
	if (!s_pocsag_1200_presync_active) return;

	const int inverted_distance = 32 - normal_distance;
	s_pocsag_1200_presync_samples++;
	if (normal_distance < s_pocsag_1200_presync_best_normal)
		s_pocsag_1200_presync_best_normal = normal_distance;
	if (inverted_distance < s_pocsag_1200_presync_best_inverted)
		s_pocsag_1200_presync_best_inverted = inverted_distance;

	if (normal_distance == 5) s_pocsag_1200_presync_normal5++;
	else if (normal_distance == 6) s_pocsag_1200_presync_normal6++;
	else if (normal_distance == 7) s_pocsag_1200_presync_normal7++;

	if (inverted_distance >= 1 && inverted_distance <= 4)
		s_pocsag_1200_presync_inverted1to4++;
	else if (inverted_distance == 5) s_pocsag_1200_presync_inverted5++;
	else if (inverted_distance == 6) s_pocsag_1200_presync_inverted6++;
	else if (inverted_distance == 7) s_pocsag_1200_presync_inverted7++;
}

static void pocsag_1200_presync_flush(const char *reason)
{
	if (!s_pocsag_1200_presync_active) return;
	fprintf(stderr,
		"[POCSAG-PRESYNC] baud=1200 samples=%lu best_normal=%d best_inverted=%d normal5=%lu normal6=%lu normal7=%lu inverted1to4=%lu inverted5=%lu inverted6=%lu inverted7=%lu reason=%s\n",
		s_pocsag_1200_presync_samples,
		s_pocsag_1200_presync_best_normal, s_pocsag_1200_presync_best_inverted,
		s_pocsag_1200_presync_normal5, s_pocsag_1200_presync_normal6, s_pocsag_1200_presync_normal7,
		s_pocsag_1200_presync_inverted1to4, s_pocsag_1200_presync_inverted5,
		s_pocsag_1200_presync_inverted6, s_pocsag_1200_presync_inverted7,
		reason ? reason : "unknown");
	fflush(stderr);
	s_pocsag_1200_presync_active = 0;
}

static void pocsag_1200_diag_begin(int sync_errors, int inverted)
{
	if (!pocsag_1200_diag_enabled() || pocsag_baud_rate != STAT_POCSAG1200) {
		s_pocsag_1200_batch_active = 0;
		return;
	}
	s_pocsag_1200_batch_active = 1;
	s_pocsag_1200_sync_errors = sync_errors;
	s_pocsag_1200_inverted = inverted;
	s_pocsag_1200_words = 0;
	s_pocsag_1200_address_words = 0;
	s_pocsag_1200_message_words = 0;
	s_pocsag_1200_bch0 = 0;
	s_pocsag_1200_bch1 = 0;
	s_pocsag_1200_bch2 = 0;
	s_pocsag_1200_bch_gt2 = 0;
}

static void pocsag_1200_diag_note_word(int errl, int is_message)
{
	if (!s_pocsag_1200_batch_active) return;
	s_pocsag_1200_words++;
	if (is_message) s_pocsag_1200_message_words++;
	else s_pocsag_1200_address_words++;

	if (errl <= 0) s_pocsag_1200_bch0++;
	else if (errl == 1) s_pocsag_1200_bch1++;
	else if (errl == 2) s_pocsag_1200_bch2++;
	else s_pocsag_1200_bch_gt2++;
}

static void pocsag_1200_diag_flush(const char *reason)
{
	if (!s_pocsag_1200_batch_active) return;
	fprintf(stderr,
		"[POCSAG-DIAG] baud=1200 sync_errors=%d inverted=%d words=%lu address=%lu message=%lu bch0=%lu bch1=%lu bch2=%lu bch_gt2=%lu reason=%s\n",
		s_pocsag_1200_sync_errors, s_pocsag_1200_inverted,
		s_pocsag_1200_words, s_pocsag_1200_address_words, s_pocsag_1200_message_words,
		s_pocsag_1200_bch0, s_pocsag_1200_bch1, s_pocsag_1200_bch2,
		s_pocsag_1200_bch_gt2, reason ? reason : "unknown");
	fflush(stderr);
	s_pocsag_1200_batch_active = 0;
}
'''

        pocsag = replace_once(
            pocsag,
            "/* RACHER_POCSAG_512_DIAG: privacy-safe pre/post-sync decoder diagnostics.\n",
            diag_state + "\n/* RACHER_POCSAG_512_DIAG: privacy-safe pre/post-sync decoder diagnostics.\n",
            "1200 diagnostic state",
        )
        pocsag = replace_once(
            pocsag,
            '\t\tpocsag_512_presync_flush("decoder-reset");\n\t\tpocsag_512_diag_flush("decoder-reset");\n',
            '\t\tpocsag_512_presync_flush("decoder-reset");\n\t\tpocsag_512_diag_flush("decoder-reset");\n'
            '\t\tpocsag_1200_presync_flush("decoder-reset");\n\t\tpocsag_1200_diag_flush("decoder-reset");\n',
            "1200 reset diagnostic flush",
        )
        pocsag = replace_once(
            pocsag,
            "\t\tif (bit >= 0) pocsag_512_presync_note(nh);\n",
            "\t\tif (bit >= 0) {\n\t\t\tpocsag_512_presync_note(nh);\n\t\t\tpocsag_1200_presync_note(nh);\n\t\t}\n",
            "1200 pre-sync distance tracking",
        )
        pocsag = replace_once(
            pocsag,
            '\t\t\tpocsag_512_presync_flush("sync-acquired");\n\t\t\tpocsag_512_diag_begin(nh, 0);\n',
            '\t\t\tpocsag_512_presync_flush("sync-acquired");\n\t\t\tpocsag_512_diag_begin(nh, 0);\n'
            '\t\t\tpocsag_1200_presync_flush("sync-acquired");\n\t\t\tpocsag_1200_diag_begin(nh, 0);\n',
            "1200 normal sync diagnostic",
        )
        pocsag = replace_once(
            pocsag,
            '\t\t\tpocsag_512_presync_flush("sync-acquired-inverted");\n\t\t\tpocsag_512_diag_begin(0, 1);\n',
            '\t\t\tpocsag_512_presync_flush("sync-acquired-inverted");\n\t\t\tpocsag_512_diag_begin(0, 1);\n'
            '\t\t\tpocsag_1200_presync_flush("sync-acquired-inverted");\n\t\t\tpocsag_1200_diag_begin(0, 1);\n',
            "1200 inverted sync diagnostic",
        )
        pocsag = replace_once(
            pocsag,
            '\t\t\tpocsag_512_diag_flush("batch-end");\n',
            '\t\t\tpocsag_512_diag_flush("batch-end");\n\t\t\tpocsag_1200_diag_flush("batch-end");\n',
            "1200 batch diagnostic flush",
        )
        pocsag = replace_once(
            pocsag,
            "\tpocsag_512_diag_note_word(errl, ob[MSB] == 1);\n",
            "\tpocsag_512_diag_note_word(errl, ob[MSB] == 1);\n\tpocsag_1200_diag_note_word(errl, ob[MSB] == 1);\n",
            "1200 BCH diagnostic count",
        )
        pocsag_path.write_text(pocsag, encoding="utf-8")
        print(f"Applied POCSAG-1200 frame diagnostics to {pocsag_path}")
    else:
        print("POCSAG-1200 frame diagnostics already applied")

    if PREAMBLE_MARKER not in decode:
        decode = replace_once(
            decode,
            "#include <windows.h>\n",
            "#include <windows.h>\n#include <stdio.h>\n#include <stdlib.h>\n",
            "1200 preamble diagnostic includes",
        )

        preamble_state = r'''
/* RACHER_POCSAG_1200_PREAMBLE_DIAG: metadata-only visibility into the
 * existing 1200-baud preamble detector. No raw symbols or message data. */
static int s_pocsag_1200_preamble_enabled = -1;
static int s_pocsag_1200_preamble_bucket = 0;
static unsigned long s_pocsag_1200_preamble_attempt = 0;

static int pocsag_1200_preamble_diag_enabled(void)
{
	if (s_pocsag_1200_preamble_enabled < 0) {
		const char *env = getenv("PDL_POCSAG_1200_DIAG");
		s_pocsag_1200_preamble_enabled = (env && env[0] && env[0] != '0') ? 1 : 0;
	}
	return s_pocsag_1200_preamble_enabled;
}

static void pocsag_1200_preamble_note(int count, int interval)
{
	if (!pocsag_1200_preamble_diag_enabled()) return;

	if (count < 10) {
		s_pocsag_1200_preamble_bucket = 0;
		return;
	}

	if (count > 180) {
		if (s_pocsag_1200_preamble_bucket == 0)
			s_pocsag_1200_preamble_attempt++;
		fprintf(stderr,
			"[POCSAG-PREAMBLE] baud=1200 attempt=%lu stage=acquired count=%d interval=%d acquired=1\n",
			s_pocsag_1200_preamble_attempt, count, interval);
		fflush(stderr);
		s_pocsag_1200_preamble_bucket = 0;
		return;
	}

	int bucket = count / 30;
	if (bucket > 6) bucket = 6;
	if (bucket <= 0 || bucket <= s_pocsag_1200_preamble_bucket) return;

	if (s_pocsag_1200_preamble_bucket == 0)
		s_pocsag_1200_preamble_attempt++;
	s_pocsag_1200_preamble_bucket = bucket;

	fprintf(stderr,
		"[POCSAG-PREAMBLE] baud=1200 attempt=%lu stage=%d count=%d interval=%d acquired=0\n",
		s_pocsag_1200_preamble_attempt, bucket * 30, count, interval);
	fflush(stderr);
}
'''
        decode = replace_once(
            decode,
            "int pocbit = 0;\t\t\t\t\t\t\t\t\t// Pocsag mode flag\n",
            "int pocbit = 0;\t\t\t\t\t\t\t\t\t// Pocsag mode flag\n" + preamble_state + "\n",
            "1200 preamble diagnostic state",
        )
        decode = replace_once(
            decode,
            "\t\t\tif ((pd_dinc > 842)  && (pd_dinc < 1142)) pd_ct12++;\n\t\t\telse if (pd_ct12 > 5) pd_ct12 -= 3;\n",
            "\t\t\tif ((pd_dinc > 842)  && (pd_dinc < 1142)) pd_ct12++;\n"
            "\t\t\telse if (pd_ct12 > 5) pd_ct12 -= 3;\n"
            "\t\t\tpocsag_1200_preamble_note(pd_ct12, pd_dinc);\n",
            "1200 preamble counter visibility",
        )
        decode_path.write_text(decode, encoding="utf-8")
        print(f"Applied POCSAG-1200 preamble diagnostics to {decode_path}")
    else:
        print("POCSAG-1200 preamble diagnostics already applied")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
