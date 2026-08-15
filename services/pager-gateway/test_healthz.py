import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


_TEST_DATA = tempfile.TemporaryDirectory()
os.environ["PAGER_DATA_DIR"] = _TEST_DATA.name
os.environ["PAGER_DB_PATH"] = os.path.join(_TEST_DATA.name, "pager-health-test.db")
os.environ["PAGER_COOKIE_SECURE"] = "0"

import app  # noqa: E402


class HealthcheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.app.config.update(TESTING=True)
        cls.client = app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        app.source.stop()
        _TEST_DATA.cleanup()

    def test_healthz_accepts_waiting_or_running_tailer_in_pdl_mode(self):
        old_state = app.source._status
        try:
            app.storage.update_settings({"source_mode": "pdl-file"})
            app.source._status = "waiting"
            response = self.client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
        finally:
            app.source._status = old_state
            app.storage.update_settings({"source_mode": "mock"})

    def test_healthz_fails_when_live_tailer_is_in_error(self):
        old_state = app.source._status
        try:
            app.storage.update_settings({"source_mode": "pdl-file"})
            app.source._status = "error"
            response = self.client.get("/healthz")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.get_json()["ok"])
        finally:
            app.source._status = old_state
            app.storage.update_settings({"source_mode": "mock"})

    def test_healthz_fails_when_database_is_unavailable(self):
        with patch.object(app.storage, "connect", side_effect=sqlite3.OperationalError("locked")):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(response.get_json()["database"], "error")


if __name__ == "__main__":
    unittest.main()
