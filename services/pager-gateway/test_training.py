from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from adaptive import AdaptiveFilter
from routing import RoutingStore
from storage import Storage
from training import TrainingStore


class TrainingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "training.db")
        self.storage = Storage(self.db)
        self.admin_id = self.storage.create_user("admin", "Admin", "hash", "admin", None)
        self.routing = RoutingStore(self.db)
        self.adaptive = AdaptiveFilter(self.db)
        self.training = TrainingStore(self.db, self.routing, self.adaptive)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def pdw(ric: str, message: str, time_value: str = "12:00:00") -> str:
        return f"{ric} {time_value} 14-08-2026 POCSAG-1 ALPHA 1200 {message}"

    def test_replay_is_isolated_and_cross_ric_duplicate_is_suppressed(self):
        text = "\n".join([
            self.pdw("1111111", "Næstved Brandvæsen BRANDALARM Industrivej 1"),
            self.pdw("2222222", "Næstved Brandvæsen BRANDALARM Industrivej 1", "12:00:05"),
        ])
        run = self.training.create_replay("Dublet test", text, self.admin_id)
        self.assertEqual(self.storage.message_count(), 0)
        self.assertEqual(run["parsed_count"], 2)
        self.assertEqual(run["real_count"], 1)
        self.assertEqual(run["duplicate_count"], 1)
        self.assertEqual(run["events"][1]["suppressed_reason"], "duplicate")
        self.assertEqual(run["events"][0]["message"], run["events"][1]["message"])
        self.assertNotEqual(run["events"][0]["ric"], run["events"][1]["ric"])
        self.assertEqual(run["events"][0]["received_at"], "2026-08-14T12:00:00")
        self.assertEqual(run["events"][1]["received_at"], "2026-08-14T12:00:05")

    def test_replay_same_text_outside_window_is_not_duplicate(self):
        message = "Næstved Brandvæsen BRANDALARM Industrivej 1"
        text = "\n".join([
            self.pdw("1111111", message, "12:00:00"),
            self.pdw("2222222", message, "12:05:00"),
        ])
        run = self.training.create_replay("Ikke dublet", text, self.admin_id, 30)
        self.assertEqual(run["parsed_count"], 2)
        self.assertEqual(run["real_count"], 2)
        self.assertEqual(run["duplicate_count"], 0)
        self.assertIsNone(run["events"][1]["suppressed_reason"])
        self.assertIsNone(run["events"][1]["duplicate_of_event_id"])

    def test_replay_uses_configurable_duplicate_window(self):
        message = "Næstved Brandvæsen BRANDALARM Industrivej 2"
        text = "\n".join([
            self.pdw("1111111", message, "12:00:00"),
            self.pdw("2222222", message, "12:00:45"),
        ])
        normal = self.training.create_replay("30 sek", text, self.admin_id, 30)
        wider = self.training.create_replay("60 sek", text, self.admin_id, 60)
        self.assertEqual(normal["duplicate_count"], 0)
        self.assertEqual(wider["duplicate_count"], 1)

    def test_replay_without_timestamps_is_not_falsely_suppressed_as_duplicate(self):
        text = "\n".join([
            "RIC: 1111111 MESSAGE: SYSTEMTEST FAST TEKST",
            "RIC: 2222222 MESSAGE: SYSTEMTEST FAST TEKST",
        ])
        run = self.training.create_replay("Ingen tid", text, self.admin_id)
        self.assertEqual(run["parsed_count"], 2)
        self.assertEqual(run["duplicate_count"], 0)
        self.assertEqual(run["real_count"], 2)

    def test_existing_training_table_is_migrated_with_received_at(self):
        legacy_tmp = tempfile.TemporaryDirectory()
        try:
            legacy_db = os.path.join(legacy_tmp.name, "legacy.db")
            legacy_storage = Storage(legacy_db)
            legacy_admin = legacy_storage.create_user("legacy", "Legacy", "hash", "admin", None)
            legacy_routing = RoutingStore(legacy_db)
            legacy_adaptive = AdaptiveFilter(legacy_db)
            with sqlite3.connect(legacy_db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE training_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        created_by INTEGER,
                        created_at TEXT NOT NULL,
                        total_lines INTEGER NOT NULL DEFAULT 0,
                        parsed_count INTEGER NOT NULL DEFAULT 0,
                        real_count INTEGER NOT NULL DEFAULT 0,
                        duplicate_count INTEGER NOT NULL DEFAULT 0,
                        noise_count INTEGER NOT NULL DEFAULT 0,
                        unknown_count INTEGER NOT NULL DEFAULT 0,
                        unclassified_count INTEGER NOT NULL DEFAULT 0,
                        station_candidate_count INTEGER NOT NULL DEFAULT 0,
                        ric_candidate_count INTEGER NOT NULL DEFAULT 0,
                        applied_at TEXT
                    );
                    CREATE TABLE training_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        line_no INTEGER NOT NULL,
                        raw_line TEXT NOT NULL,
                        message TEXT NOT NULL,
                        ric TEXT,
                        station TEXT,
                        routing_source TEXT NOT NULL DEFAULT 'unknown',
                        relevance_class TEXT NOT NULL DEFAULT 'unknown',
                        relevance_score REAL NOT NULL DEFAULT 0.75,
                        suppressed_reason TEXT,
                        duplicate_of_event_id INTEGER,
                        decision_reason TEXT NOT NULL DEFAULT '',
                        feedback TEXT
                    );
                    """
                )
            TrainingStore(legacy_db, legacy_routing, legacy_adaptive)
            with sqlite3.connect(legacy_db) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(training_events)").fetchall()}
            self.assertIn("received_at", columns)
            self.assertIsNotNone(legacy_admin)
        finally:
            legacy_tmp.cleanup()

    def test_replay_builds_station_and_ric_candidates_without_mutating_live_routing(self):
        text = "\n".join([
            self.pdw("3333333", "Næstved Brandvæsen BRANDALARM Test 1", "12:01:00"),
            self.pdw("3333333", "Næstved Brandvæsen BRANDALARM Test 2", "12:02:00"),
            self.pdw("3333333", "Næstved Brandvæsen BRANDALARM Test 3", "12:03:00"),
        ])
        run = self.training.create_replay("Næstved discovery", text, self.admin_id)
        self.assertIsNone(self.routing.station_key("Næstved"))
        self.assertEqual(run["station_candidate_count"], 1)
        self.assertEqual(run["station_candidates"][0]["station_name"], "Næstved")
        self.assertEqual(run["ric_candidate_count"], 1)
        self.assertEqual(run["ric_candidates"][0]["ric"], "3333333")
        self.assertEqual(run["ric_candidates"][0]["station_name"], "Næstved")

    def test_approved_replay_learning_creates_station_ric_and_noise_votes(self):
        message = "Næstved Brandvæsen SYSTEMTEST FAST TEKST"
        text = "\n".join([
            self.pdw("4444444", message, "13:00:00"),
            self.pdw("4444444", message, "13:00:05"),
            self.pdw("4444444", message, "13:00:10"),
        ])
        run = self.training.create_replay("Apply learning", text, self.admin_id)
        for event in run["events"]:
            self.training.set_event_feedback(event["id"], "noise")
        self.training.set_candidate_decisions(
            run["id"],
            [{"station_name": "Næstved", "decision": "approved"}],
            [{"ric": "4444444", "station_name": "Næstved", "decision": "approved"}],
        )
        result = self.training.apply_run(run["id"])
        self.assertEqual(result["stations_created"], 1)
        self.assertEqual(result["rics_created"], 1)
        self.assertEqual(result["feedback_applied"], 3)
        self.assertIsNotNone(self.routing.station_key("Næstved"))
        station, source = self.routing.classify("4444444", None, "andet indhold")
        self.assertEqual(station, "Næstved")
        self.assertEqual(source, "ric")
        learned = self.adaptive.learned_relevance(message)
        self.assertEqual(learned["classification"], "noise")
        with self.assertRaises(ValueError):
            self.training.apply_run(run["id"])

    def test_apply_uses_feedback_beyond_first_1000_displayed_events(self):
        text = "\n".join(
            self.pdw("8888888", f"SYSTEMTEST MELDING {index}", "14:00:00")
            for index in range(1001)
        )
        run = self.training.create_replay("Large replay", text, self.admin_id)
        self.assertEqual(run["parsed_count"], 1001)
        self.assertEqual(len(run["events"]), 1000)
        with self.training.connect() as conn:
            last = conn.execute(
                "SELECT id, message FROM training_events WHERE run_id=? ORDER BY line_no DESC LIMIT 1",
                (run["id"],),
            ).fetchone()
        self.training.set_event_feedback(last["id"], "noise")
        result = self.training.apply_run(run["id"])
        self.assertEqual(result["feedback_applied"], 1)
        learned = self.adaptive.learned_relevance(last["message"])
        self.assertEqual(learned["classification"], "unknown")

    def test_bulk_ric_import_preview_and_apply(self):
        text = (
            "RIC;Område;Beskrivelse;Aktiv\n"
            "5555555;Ringsted;Primær alarm;1\n"
            "6666666;Holbæk;Sekundær;ja\n"
            "BAD;Kalundborg;Fejl;1\n"
        )
        preview = self.training.preview_ric_import(text)
        self.assertEqual(len(preview["rows"]), 2)
        self.assertEqual(len(preview["errors"]), 1)
        self.assertFalse(preview["rows"][0]["station_exists"])

        result = self.training.apply_ric_import(text, True, self.admin_id)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["stations_created"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIsNotNone(self.routing.station_key("Ringsted"))
        self.assertIsNotNone(self.routing.station_key("Holbæk"))

        second = self.training.apply_ric_import(text, True, self.admin_id)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped_existing"], 2)

    def test_import_without_missing_station_creation_reports_error(self):
        text = "7777777;Kalundborg;Test;1"
        result = self.training.apply_ric_import(text, False, self.admin_id)
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIsNone(self.routing.station_key("Kalundborg"))


if __name__ == "__main__":
    unittest.main()
