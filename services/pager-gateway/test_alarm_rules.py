from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, g, jsonify, request

from alarm_rules import (
    AlarmFilterStore,
    _clean_pager_message,
    _find_extended_duplicate,
    _quality_noise_reason,
    alarm_clock,
    install_alarm_rules,
    match_filter_term,
    normalize_filter_terms,
)


class AlarmRuleTests(unittest.TestCase):
    def test_normalize_terms_splits_and_deduplicates_case_insensitively(self):
        self.assertEqual(
            normalize_filter_terms("TEST, øvelse;  Service besked\n test "),
            ["TEST", "øvelse", "Service besked"],
        )

    def test_match_term_is_case_insensitive_and_supports_phrases(self):
        self.assertEqual(match_filter_term("Dette er en ØVELSE ved stationen", ["test", "øvelse"]), "øvelse")
        self.assertEqual(match_filter_term("Planlagt SERVICE BESKED", ["service besked"]), "service besked")
        self.assertIsNone(match_filter_term("Rigtig brandalarm", ["test", "øvelse"]))

    def test_alarm_clock_preserves_pdw_wall_clock_and_converts_utc(self):
        self.assertEqual(alarm_clock("2026-08-17T20:59:12"), "20:59:12")
        old = os.environ.get("PAGER_TIMEZONE")
        os.environ["PAGER_TIMEZONE"] = "Europe/Copenhagen"
        try:
            self.assertEqual(alarm_clock("2026-08-17T18:59:12+00:00"), "20:59:12")
        finally:
            if old is None:
                os.environ.pop("PAGER_TIMEZONE", None)
            else:
                os.environ["PAGER_TIMEZONE"] = old

    def test_store_replaces_terms_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "pager.db")
            store = AlarmFilterStore(db)
            self.assertEqual(store.replace_terms(["TEST", "øvelse"]), ["TEST", "øvelse"])
            self.assertEqual(store.list_terms(), ["TEST", "øvelse"])
            self.assertEqual(store.match("Alarm - ØVELSE"), "øvelse")
            self.assertEqual(store.replace_terms([]), [])
            self.assertEqual(store.list_terms(), [])

    def test_known_operational_prefixes_are_preserved(self):
        first = "@8 NR RI(1+5)M+S Ringsted Svømmeland BRANDALARM 4100 Ringsted"
        second = "$9 ISL-Forespørgsel 4100 Ringsted lugt af brændt plastic"
        self.assertEqual(_clean_pager_message(first), first)
        self.assertEqual(_clean_pager_message(second), second)

    def test_short_nr_dispatch_copy_is_treated_as_partial(self):
        text = "NR RI(1+5)M+S · Ringsted Svlmv2v"
        self.assertEqual(_quality_noise_reason(text), "decoder-partial")
        complete = "@8 NR RI(1+5)M+S Ringsted Svømmeland BRANDALARM 4100 Ringsted"
        self.assertIsNone(_quality_noise_reason(complete))

    @staticmethod
    def _create_message_table(db: str) -> None:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       received_at TEXT,
                       message TEXT,
                       delivery_eligible INTEGER,
                       duplicate_of INTEGER
                   )"""
            )
            conn.commit()

    def test_nr_dispatch_burst_dedupes_observed_corrupt_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "pager.db")
            store = AlarmFilterStore(db)
            self._create_message_table(db)
            with sqlite3.connect(db) as conn:
                cur = conn.execute(
                    "INSERT INTO messages(received_at, message, delivery_eligible, duplicate_of) VALUES (?, ?, 1, NULL)",
                    (
                        "2026-08-20T12:35:35",
                        "@8 NR RI(1+5)M+S · Ringsted(?vømmeland · BRANDALARM · 4100 Ringsted",
                    ),
                )
                first_id = int(cur.lastrowid)
                conn.commit()

            # RIC 0006220: the RI(...) prefix and first Ringsted character are corrupt.
            duplicate = _find_extended_duplicate(
                store,
                "@8 NR RM*1+5)M+S · R`ngsted Svømmeland · BRANDALARM · 4100 Ringsted",
                "2026-08-20T12:35:36",
            )
            self.assertEqual(duplicate, first_id)

            # RIC 0006240: place/postcode/town contain several bit errors.
            duplicate = _find_extended_duplicate(
                store,
                "@8 NR RI(1+5)M+S · Ringstul Svømmeland · BRANDALARM · 410x Zingsted",
                "2026-08-20T12:35:39",
            )
            self.assertEqual(duplicate, first_id)

    def test_nr_dispatch_dedupe_does_not_hide_different_same_town_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "pager.db")
            store = AlarmFilterStore(db)
            self._create_message_table(db)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO messages(received_at, message, delivery_eligible, duplicate_of) VALUES (?, ?, 1, NULL)",
                    (
                        "2026-08-20T12:35:35",
                        "@8 NR RI(1+5)M+S · Ringsted Svømmeland · BRANDALARM · 4100 Ringsted",
                    ),
                )
                conn.commit()

            duplicate = _find_extended_duplicate(
                store,
                "@8 NR RI(1+5)M+S · Ringsted Station · BRANDALARM · 4100 Ringsted",
                "2026-08-20T12:35:39",
            )
            self.assertIsNone(duplicate)

    def test_installed_filter_suppresses_before_original_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            observed = []
            delivered = []

            class FakeStorage:
                def add_message(self, data):
                    captured.append(dict(data))
                    return 77

                def add_audit(self, *args, **kwargs):
                    return None

            class FakeAdaptive:
                @staticmethod
                def exact_signature(message):
                    return "sig:" + message

                @staticmethod
                def observe(message_id, message):
                    observed.append((message_id, message))

            class FakeRouting:
                @staticmethod
                def classify(ric, station, message):
                    return station or "Slagelse", "test"

            class Event:
                message = "TEST alarm fra station"

                @staticmethod
                def to_dict():
                    return {
                        "message": "TEST alarm fra station",
                        "raw_line": "raw",
                        "source": "mock",
                        "protocol": "POCSAG",
                        "received_at": "2026-08-17T20:59:12",
                    }

            app = Flask(__name__)

            def auth_required(admin=False):
                return lambda fn: fn

            def original_ingest(event):
                delivered.append(event.message)
                return 99

            def original_pushover(message_id, event):
                delivered.append(event["message"])

            def original_web_push(message_id, event):
                delivered.append(event["message"])

            core = SimpleNamespace(
                app=app,
                auth_required=auth_required,
                jsonify=jsonify,
                request=request,
                g=g,
                DB_PATH=str(Path(tmp) / "pager.db"),
                public_message=lambda value: str(value).strip(),
                adaptive=FakeAdaptive(),
                routing=FakeRouting(),
                storage=FakeStorage(),
                ingest_event=original_ingest,
                maybe_notify_pushover=original_pushover,
                send_web_push_for_event=original_web_push,
            )

            store = install_alarm_rules(core)
            store.replace_terms(["test"])
            self.assertEqual(core.ingest_event(Event()), 77)
            self.assertEqual(delivered, [])
            self.assertEqual(captured[0]["delivery_eligible"], False)
            self.assertEqual(captured[0]["suppressed_reason"], "word-filter:test")
            self.assertEqual(observed, [(77, "TEST alarm fra station")])

            Event.message = "Rigtig brandalarm"
            self.assertEqual(core.ingest_event(Event()), 99)
            self.assertEqual(delivered, ["Rigtig brandalarm"])

    def test_notification_wrappers_add_alarm_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            pushed = []
            app = Flask(__name__)

            core = SimpleNamespace(
                app=app,
                auth_required=lambda admin=False: (lambda fn: fn),
                jsonify=jsonify,
                request=request,
                g=g,
                DB_PATH=str(Path(tmp) / "pager.db"),
                public_message=lambda value: str(value).strip(),
                adaptive=SimpleNamespace(exact_signature=lambda value: value, observe=lambda *args: None),
                routing=SimpleNamespace(classify=lambda ric, station, message: (station, "test")),
                storage=SimpleNamespace(add_message=lambda data: 1, add_audit=lambda *args: None),
                ingest_event=lambda event: 1,
                maybe_notify_pushover=lambda message_id, event: pushed.append(("pushover", event["message"])),
                send_web_push_for_event=lambda message_id, event: pushed.append(("push", event["message"])),
            )
            install_alarm_rules(core)
            event = {"message": "Brandalarm", "received_at": "2026-08-17T20:59:12"}
            core.maybe_notify_pushover(1, event)
            core.send_web_push_for_event(1, event)
            self.assertEqual(pushed[0][1], "Alarmtid 20:59:12\nBrandalarm")
            self.assertEqual(pushed[1][1], "Alarmtid 20:59:12\nBrandalarm")


if __name__ == "__main__":
    unittest.main()
