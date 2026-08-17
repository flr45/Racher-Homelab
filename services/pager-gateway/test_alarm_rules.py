from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, g, jsonify, request

from alarm_rules import AlarmFilterStore, alarm_clock, install_alarm_rules, match_filter_term, normalize_filter_terms


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
