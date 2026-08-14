from __future__ import annotations

import os
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
