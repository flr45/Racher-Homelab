from __future__ import annotations

import os
import tempfile
import unittest


_TEST_DATA = tempfile.TemporaryDirectory()
os.environ["PAGER_DATA_DIR"] = _TEST_DATA.name
os.environ["PAGER_DB_PATH"] = os.path.join(_TEST_DATA.name, "pager-test.db")
os.environ["PAGER_COOKIE_SECURE"] = "0"

from app import app, routing, storage  # noqa: E402


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        cls.admin = app.test_client()

        cls.admin.get("/setup")
        with cls.admin.session_transaction() as sess:
            csrf = sess["csrf_token"]
        response = cls.admin.post(
            "/setup",
            data={
                "csrf_token": csrf,
                "display_name": "Admin",
                "username": "admin",
                "password": "meget-hemmelig-admin",
            },
        )
        assert response.status_code == 302

        with cls.admin.session_transaction() as sess:
            cls.admin_csrf = sess["csrf_token"]

        response = cls.admin.post(
            "/api/users",
            json={
                "display_name": "Alarmbruger",
                "username": "alarmuser",
                "password": "meget-hemmelig-user",
                "role": "user",
                "stations": ["A"],
            },
            headers={"X-CSRF-Token": cls.admin_csrf},
        )
        assert response.status_code == 200

        cls.user = app.test_client()
        cls.user.get("/login")
        with cls.user.session_transaction() as sess:
            csrf = sess["csrf_token"]
        response = cls.user.post(
            "/login",
            data={"csrf_token": csrf, "username": "alarmuser", "password": "meget-hemmelig-user"},
        )
        assert response.status_code == 302
        with cls.user.session_transaction() as sess:
            cls.user_csrf = sess["csrf_token"]

    @classmethod
    def tearDownClass(cls):
        _TEST_DATA.cleanup()

    def test_first_setup_is_closed_after_admin_exists(self):
        response = self.user.get("/setup")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_user_ui_only_exposes_alarm_features(self):
        response = self.user.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Alarmer", page)
        self.assertIn("Historik", page)
        self.assertNotIn('data-tab="system"', page)
        self.assertNotIn('data-tab="users"', page)
        self.assertNotIn('data-tab="ric"', page)
        self.assertNotIn('data-tab="settings"', page)
        self.assertNotIn("Send testalarm", page)
        self.assertNotIn('id="readiness-list"', page)
        self.assertNotIn("Network mobility", page)
        self.assertNotIn("Backup & recovery", page)
        self.assertNotIn("Update & rollback", page)

    def test_admin_ui_contains_remote_appliance_and_routing_features(self):
        response = self.admin.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('data-tab="system"', page)
        self.assertIn('data-tab="users"', page)
        self.assertIn('data-tab="ric"', page)
        self.assertIn('data-tab="settings"', page)
        self.assertIn("RIC / Capcode", page)
        self.assertIn("Stationer pr. bruger", page)
        self.assertIn("Send testalarm", page)
        self.assertIn('id="readiness-list"', page)
        self.assertIn("Raspberry Pi-status", page)
        self.assertIn("Network mobility", page)
        self.assertIn("Backup & recovery", page)
        self.assertIn("Update & rollback", page)
        self.assertIn("Cloudflare Tunnel", page)

    def test_admin_status_contains_readiness_but_user_is_forbidden(self):
        response = self.admin.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("runtime", payload)
        self.assertIn("readiness", payload)
        self.assertTrue(any(item["key"] == "gateway" for item in payload["readiness"]))
        self.assertTrue(any(item["key"] == "network" for item in payload["readiness"]))
        self.assertTrue(any(item["key"] == "fsk-usb" for item in payload["readiness"]))
        self.assertEqual(self.user.get("/api/status").status_code, 403)

    def test_user_can_read_only_routed_alarms(self):
        self.storage_message("Slagelse", "A-routed")
        self.storage_message("Sorø", "S-hidden")
        response = self.user.get("/api/messages")
        self.assertEqual(response.status_code, 200)
        messages = [item["message"] for item in response.get_json()]
        self.assertIn("A-routed", messages)
        self.assertNotIn("S-hidden", messages)

    @staticmethod
    def storage_message(station: str, text: str) -> None:
        storage.add_message({
            "protocol": "POCSAG", "message": text, "raw_line": text,
            "source": "test-auth", "station": station,
        })

    def test_user_cannot_read_admin_settings_users_status_audit_or_rics(self):
        self.assertEqual(self.user.get("/api/settings").status_code, 403)
        self.assertEqual(self.user.get("/api/users").status_code, 403)
        self.assertEqual(self.user.get("/api/status").status_code, 403)
        self.assertEqual(self.user.get("/api/audit").status_code, 403)
        self.assertEqual(self.user.get("/api/stations").status_code, 403)
        self.assertEqual(self.user.get("/api/ric-codes").status_code, 403)
        self.assertEqual(self.user.get("/api/ric-codes/unknown").status_code, 403)

    def test_user_cannot_create_users_rics_or_system_commands(self):
        response = self.user.post(
            "/api/users",
            json={"username": "forbidden", "password": "1234567890", "role": "user"},
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

        response = self.user.post(
            "/api/ric-codes",
            json={"ric": "9999999", "station_key": "A"},
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

        response = self.user.post(
            "/api/system/commands",
            json={
                "action": "wifi-add",
                "payload": {"ssid": "Station WiFi", "password": "12345678"},
            },
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

        response = self.user.post(
            "/api/mock",
            json={"message": "test"},
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_and_edit_ric_mapping(self):
        response = self.admin.post(
            "/api/ric-codes",
            json={"ric": "9876543", "station_key": "A", "label": "Test RIC", "active": True},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)
        ric_id = response.get_json()["id"]
        self.assertEqual(routing.get_ric_code(ric_id)["station"], "Slagelse")

        response = self.admin.patch(
            f"/api/ric-codes/{ric_id}",
            json={"station_key": "S", "label": "Flyttet"},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ric"]["station"], "Sorø")

    def test_admin_can_change_user_station_routing(self):
        user = storage.get_user_by_username("alarmuser")
        response = self.admin.patch(
            f"/api/users/{user['id']}",
            json={"stations": ["A", "K"]},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(routing.user_stations(user["id"])), {"A", "K"})
        # Restore fixture expectation for tests that may run after this one.
        routing.set_user_stations(user["id"], ["A"])

    def test_admin_can_queue_validated_wifi_action(self):
        response = self.admin.post(
            "/api/system/commands",
            json={
                "action": "wifi-add",
                "payload": {"ssid": "Station WiFi", "password": "12345678"},
            },
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)

        invalid = self.admin.post(
            "/api/system/commands",
            json={
                "action": "wifi-add",
                "payload": {"ssid": "Station WiFi", "password": "kort"},
            },
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_admin_can_create_user_and_queue_whitelisted_action(self):
        response = self.admin.post(
            "/api/users",
            json={
                "display_name": "Ekstra bruger",
                "username": "extrauser",
                "password": "1234567890-ekstra",
                "role": "user",
                "stations": ["S"],
            },
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)
        created = storage.get_user_by_username("extrauser")
        self.assertIsNotNone(created)
        self.assertEqual(routing.user_stations(created["id"]), ["S"])

        response = self.admin.post(
            "/api/system/commands",
            json={"action": "restart-pdl"},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)

    def test_system_action_whitelist_rejects_arbitrary_commands(self):
        response = self.admin.post(
            "/api/system/commands",
            json={"action": "rm-everything", "payload": {"command": "rm -rf /"}},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 400)

    def test_csrf_is_required_for_mutations(self):
        response = self.admin.post("/api/system/commands", json={"action": "restart-pdl"})
        self.assertEqual(response.status_code, 400)
        response = self.admin.post("/api/ric-codes", json={"ric": "5555555", "station_key": "A"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
