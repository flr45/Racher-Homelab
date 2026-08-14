from __future__ import annotations

import os
import tempfile
import unittest

from routing import ALL_STATION_KEYS, RoutingStore
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

    def test_existing_admin_is_seeded_with_all_stations(self):
        admin_id = self._user("admin", "admin")
        routing = RoutingStore(self.db)
        self.assertEqual(set(routing.user_stations(admin_id)), set(ALL_STATION_KEYS))

    def test_ric_mapping_wins_over_text_marker(self):
        admin_id = self._user("admin", "admin")
        routing = RoutingStore(self.db)
        routing.create_ric_code("1234567", "A", "Slagelse primær", True, admin_id)
        station, source = routing.classify("1234567", "Sorø")
        self.assertEqual(station, "Slagelse")
        self.assertEqual(source, "ric")

    def test_marker_is_fallback_for_unknown_ric(self):
        self._user("admin", "admin")
        routing = RoutingStore(self.db)
        station, source = routing.classify("7654321", "Korsør")
        self.assertEqual(station, "Korsør")
        self.assertEqual(source, "marker")

    def test_unknown_ric_is_kept_and_can_be_assigned_later(self):
        admin_id = self._user("admin", "admin")
        routing = RoutingStore(self.db)
        self.storage.add_message({
            "protocol": "POCSAG", "ric": "7654321", "message": "Ukendt test",
            "raw_line": "Ukendt test", "source": "test", "station": None,
        })
        unknown = routing.list_unknown_rics()
        self.assertEqual(unknown[0]["ric"], "7654321")

        routing.create_ric_code("7654321", "S", "Sorø", True, admin_id)
        self.assertEqual(routing.list_unknown_rics(), [])
        self.assertEqual(self.storage.latest_message()["station"], "Sorø")

    def test_user_feed_is_limited_to_selected_stations(self):
        self._user("admin", "admin")
        user_id = self._user("brandmand")
        routing = RoutingStore(self.db)
        routing.set_user_stations(user_id, ["A"])
        self.storage.add_message({
            "protocol": "POCSAG", "ric": "1111111", "message": "Slagelse alarm",
            "raw_line": "Slagelse alarm", "source": "test", "station": "Slagelse",
        })
        self.storage.add_message({
            "protocol": "POCSAG", "ric": "2222222", "message": "Sorø alarm",
            "raw_line": "Sorø alarm", "source": "test", "station": "Sorø",
        })
        rows = routing.list_messages_for_user(user_id)
        self.assertEqual([row["message"] for row in rows], ["Slagelse alarm"])

    def test_push_routing_uses_station_subscriptions_and_unknown_goes_to_admin(self):
        admin_id = self._user("admin", "admin")
        slagelse_id = self._user("slagelse")
        soro_id = self._user("soro")
        routing = RoutingStore(self.db)
        routing.set_user_stations(slagelse_id, ["A"])
        routing.set_user_stations(soro_id, ["S"])
        self.storage.upsert_push_subscription(admin_id, "https://push/admin", "a", "b")
        self.storage.upsert_push_subscription(slagelse_id, "https://push/slagelse", "a", "b")
        self.storage.upsert_push_subscription(soro_id, "https://push/soro", "a", "b")

        slagelse = routing.list_push_subscriptions_for_event("Slagelse")
        self.assertEqual(
            {item["endpoint"] for item in slagelse},
            {"https://push/admin", "https://push/slagelse"},
        )
        unknown = routing.list_push_subscriptions_for_event(None)
        self.assertEqual({item["endpoint"] for item in unknown}, {"https://push/admin"})

    def test_invalid_ric_and_station_are_rejected(self):
        self._user("admin", "admin")
        routing = RoutingStore(self.db)
        with self.assertRaises(ValueError):
            routing.normalize_ric("ABC")
        with self.assertRaises(ValueError):
            routing.set_user_stations(1, ["X"])


if __name__ == "__main__":
    unittest.main()
