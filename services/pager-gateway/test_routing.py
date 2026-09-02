from __future__ import annotations

import os
import tempfile
import unittest

from routing import RoutingStore
from storage import Storage


class RoutingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "routing.db")
        self.storage = Storage(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _user(self, username: str, role: str = "user") -> int:
        return self.storage.create_user(username, username.title(), "hash", role, None)

    def test_existing_admin_is_seeded_receive_all(self):
        admin_id = self._user("admin", "admin")
        routing = RoutingStore(self.db)
        self.assertTrue(routing.user_receive_all(admin_id))
        self.assertGreaterEqual(len(routing.list_stations()), 5)

    def test_dynamic_station_can_be_created_without_code_change(self):
        self._user("admin", "admin")
        routing = RoutingStore(self.db)
        row = routing.create_station("Næstved", source="admin")
        self.assertEqual(row["name"], "Næstved")
        self.assertIn(row["key"], routing.all_station_keys())

    def test_ric_mapping_wins_over_text_marker(self):
        admin_id = self._user("admin", "admin")
        routing = RoutingStore(self.db)
        routing.create_ric_code("1234567", "A", "Slagelse primær", True, admin_id)
        station, source = routing.classify("1234567", "Sorø", "(S) test")
        self.assertEqual(station, "Slagelse")
        self.assertEqual(source, "ric")

    def test_marker_is_fallback_for_unknown_ric(self):
        self._user("admin", "admin")
        routing = RoutingStore(self.db)
        station, source = routing.classify("7654321", "Korsør", "(K) test")
        self.assertEqual(station, "Korsør")
        self.assertEqual(source, "marker")

    def test_unknown_ric_is_kept_and_can_be_assigned_later(self):
        admin_id = self._user("admin", "admin")
        routing = RoutingStore(self.db)
        self.storage.add_message({
            "protocol": "POCSAG", "ric": "7654321", "message": "Ukendt test",
            "raw_line": "Ukendt test", "source": "test", "station": None,
        })
        self.assertEqual(routing.list_unknown_rics()[0]["ric"], "7654321")
        routing.create_ric_code("7654321", "S", "Sorø", True, admin_id)
        self.assertEqual(routing.list_unknown_rics(), [])
        self.assertEqual(self.storage.latest_message()["station"], "Sorø")

    def test_user_feed_hides_private_ric_and_raw_line(self):
        self._user("admin", "admin")
        user_id = self._user("brandmand")
        routing = RoutingStore(self.db)
        routing.set_user_stations(user_id, ["A"])
        self.storage.add_message({
            "protocol": "POCSAG", "ric": "1111111", "message": "Slagelse alarm",
            "raw_line": "1111111 raw", "source": "test", "station": "Slagelse",
        })
        rows = routing.list_messages_for_user(user_id)
        self.assertEqual(rows[0]["message"], "Slagelse alarm")
        self.assertNotIn("ric", rows[0])
        self.assertNotIn("raw_line", rows[0])
        self.assertNotIn("function", rows[0])

    def test_all_messages_gets_every_delivery_eligible_area_but_not_noise(self):
        self._user("admin", "admin")
        user_id = self._user("alluser")
        routing = RoutingStore(self.db)
        routing.set_user_receive_all(user_id, True)
        self.storage.add_message({
            "message": "Holbæk rigtig", "raw_line": "Holbæk rigtig", "source": "test",
            "station": "Holbæk", "delivery_eligible": True,
        })
        self.storage.add_message({
            "message": "Teknisk støj", "raw_line": "Teknisk støj", "source": "test",
            "station": None, "delivery_eligible": False, "suppressed_reason": "noise",
        })
        rows = routing.list_messages_for_user(user_id)
        self.assertEqual([row["message"] for row in rows], ["Holbæk rigtig"])

    def test_push_routing_uses_area_subscriptions_and_receive_all(self):
        admin_id = self._user("admin", "admin")
        slagelse_id = self._user("slagelse")
        all_id = self._user("alle")
        routing = RoutingStore(self.db)
        routing.set_user_stations(slagelse_id, ["A"])
        routing.set_user_receive_all(all_id, True)
        self.storage.upsert_push_subscription(admin_id, "https://push/admin", "a", "b")
        self.storage.upsert_push_subscription(slagelse_id, "https://push/slagelse", "a", "b")
        self.storage.upsert_push_subscription(all_id, "https://push/all", "a", "b")
        slagelse = routing.list_push_subscriptions_for_event("Slagelse", True)
        self.assertEqual(
            {item["endpoint"] for item in slagelse},
            {"https://push/admin", "https://push/slagelse", "https://push/all"},
        )
        self.assertEqual(routing.list_push_subscriptions_for_event("Slagelse", False), [])

    def test_explicit_station_logic_can_auto_create_new_area(self):
        self._user("admin", "admin")
        routing = RoutingStore(self.db)
        # Strong explicit wording is repeated; mere incident place names are not enough.
        for _ in range(3):
            station, _source = routing.classify(None, None, "Næstved Brandvæsen - testmelding")
        self.assertEqual(station, "Næstved")
        created = [row for row in routing.list_stations() if row["name"] == "Næstved"]
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]["auto_created"])

    def test_invalid_ric_and_station_are_rejected(self):
        self._user("admin", "admin")
        routing = RoutingStore(self.db)
        with self.assertRaises(ValueError):
            routing.normalize_ric("ABC")
        with self.assertRaises(ValueError):
            routing.set_user_stations(1, ["DOES-NOT-EXIST"])


if __name__ == "__main__":
    unittest.main()
