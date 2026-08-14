from __future__ import annotations

import os
import tempfile
import unittest


_TEST_DATA = tempfile.TemporaryDirectory()
os.environ["PAGER_DATA_DIR"] = _TEST_DATA.name
os.environ["PAGER_DB_PATH"] = os.path.join(_TEST_DATA.name, "pager-test.db")
os.environ["PAGER_COOKIE_SECURE"] = "0"

from app import app, storage  # noqa: E402


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
        self.assertNotIn('data-tab="settings"', page)
        self.assertNotIn("Send testalarm", page)
        self.assertNotIn('id="readiness-list"', page)

    def test_admin_ui_contains_admin_features(self):
        response = self.admin.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('data-tab="system"', page)
        self.assertIn('data-tab="users"', page)
        self.assertIn('data-tab="settings"', page)
        self.assertIn("Send testalarm", page)
        self.assertIn('id="readiness-list"', page)
        self.assertIn("Raspberry Pi-status", page)

    def test_admin_status_contains_readiness_but_user_is_forbidden(self):
        response = self.admin.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("runtime", payload)
        self.assertIn("readiness", payload)
        self.assertTrue(any(item["key"] == "gateway" for item in payload["readiness"]))
        self.assertEqual(self.user.get("/api/status").status_code, 403)

    def test_user_can_read_alarms(self):
        response = self.user.get("/api/messages")
        self.assertEqual(response.status_code, 200)

    def test_user_cannot_read_admin_settings_or_users(self):
        self.assertEqual(self.user.get("/api/settings").status_code, 403)
        self.assertEqual(self.user.get("/api/users").status_code, 403)
        self.assertEqual(self.user.get("/api/status").status_code, 403)

    def test_user_cannot_create_users_or_system_commands(self):
        response = self.user.post(
            "/api/users",
            json={"username": "forbidden", "password": "1234567890", "role": "user"},
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

        response = self.user.post(
            "/api/system/commands",
            json={"action": "reboot"},
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

        response = self.user.post(
            "/api/mock",
            json={"message": "test"},
            headers={"X-CSRF-Token": self.user_csrf},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_user_and_queue_whitelisted_action(self):
        response = self.admin.post(
            "/api/users",
            json={
                "display_name": "Ekstra bruger",
                "username": "extrauser",
                "password": "1234567890-ekstra",
                "role": "user",
            },
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(storage.get_user_by_username("extrauser"))

        response = self.admin.post(
            "/api/system/commands",
            json={"action": "restart-pdl"},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200)

    def test_system_action_whitelist_rejects_arbitrary_commands(self):
        response = self.admin.post(
            "/api/system/commands",
            json={"action": "rm-everything"},
            headers={"X-CSRF-Token": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 400)

    def test_csrf_is_required_for_mutations(self):
        response = self.admin.post("/api/system/commands", json={"action": "restart-pdl"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
