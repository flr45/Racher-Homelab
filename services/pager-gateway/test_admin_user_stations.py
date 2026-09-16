from __future__ import annotations

import os
import tempfile
import unittest

from admin_user_stations import AdminUserStationStore
from routing import RoutingStore
from storage import Storage


class AdminUserStationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db_path)
        self.routing = RoutingStore(self.db_path)
        self.admin_id = self.storage.create_user(
            "admin-test", "Admin Test", "unused-hash", "admin"
        )
        self.user_id = self.storage.create_user(
            "user-test", "Bruger Test", "unused-hash", "user", self.admin_id
        )
        self.store = AdminUserStationStore(self.storage)

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_station_assignment_is_separate_from_alarm_routing(self):
        self.routing.set_user_stations(self.user_id, ["A"])
        station = self.store.create_station("Station Slagelse", self.admin_id)
        assignment = self.store.set_user_station(
            self.user_id, station["id"], self.admin_id
        )

        self.assertEqual(assignment["station_name"], "Station Slagelse")
        self.assertEqual(self.routing.user_stations(self.user_id), ["A"])

        overview = self.store.list_overview()
        self.assertEqual(overview["stations"][0]["user_count"], 1)
        self.assertEqual(overview["assignments"][0]["user_id"], self.user_id)
        self.assertEqual(overview["assignments"][0]["station_id"], station["id"])

    def test_existing_users_can_remain_without_admin_station(self):
        station = self.store.create_station("Station Korsør", self.admin_id)
        overview = self.store.list_overview()
        self.assertEqual(overview["stations"][0]["id"], station["id"])
        self.assertEqual(overview["stations"][0]["user_count"], 0)
        self.assertEqual(overview["assignments"], [])

    def test_user_can_be_moved_back_to_without_station(self):
        station = self.store.create_station("Station Skælskør", self.admin_id)
        self.store.set_user_station(self.user_id, station["id"], self.admin_id)
        result = self.store.set_user_station(self.user_id, None, self.admin_id)
        self.assertIsNone(result["station_id"])
        self.assertEqual(self.store.list_overview()["assignments"], [])

    def test_station_names_are_unique_case_insensitively(self):
        self.store.create_station("Station Slagelse", self.admin_id)
        with self.assertRaisesRegex(ValueError, "allerede"):
            self.store.create_station("  station   slagelse ", self.admin_id)

    def test_tables_do_not_reuse_alarm_routing_station_tables(self):
        self.store.create_station("Station Slagelse", self.admin_id)
        with self.storage.connect() as conn:
            table_names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("stations", table_names)
        self.assertIn("user_station_subscriptions", table_names)
        self.assertIn("admin_user_stations", table_names)
        self.assertIn("admin_user_station_memberships", table_names)


if __name__ == "__main__":
    unittest.main()
