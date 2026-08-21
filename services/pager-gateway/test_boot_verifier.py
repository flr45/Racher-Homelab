from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import boot_verifier


class BootVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "CREATE TABLE runtime_status (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO runtime_status(key,value,updated_at) VALUES (?,?,'now')",
                [
                    ("fsk_usb_connected", "1"),
                    ("fsk_usb_pdl_in_use", "1"),
                ],
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_to_end_boot_check_can_become_ready(self):
        def fake_http(url, timeout=0):
            if url.endswith("/healthz"):
                return {"ok": True}
            return {"status": "ok", "modem": {"state": "online"}}

        with patch.object(boot_verifier, "DB_PATH", self.db), \
             patch.object(boot_verifier, "SMS_GATEWAY_URL", "http://100.111.28.12:8090"), \
             patch.object(boot_verifier, "service_active", return_value=True), \
             patch.object(boot_verifier, "http_json", side_effect=fake_http), \
             patch.object(boot_verifier, "tailscale_status", return_value={"installed": True, "service": "active", "ip": "100.81.169.71"}):
            result = boot_verifier.check_once()

        self.assertTrue(result["local_ready"])
        self.assertTrue(result["remote_ready"])
        self.assertTrue(result["end_to_end_ready"])
        self.assertTrue(result["checks"]["tailscale"])
        self.assertTrue(result["checks"]["gsm_modem"])

    def test_result_is_persisted_for_dashboard(self):
        result = {
            "local_ready": True,
            "remote_ready": True,
            "end_to_end_ready": True,
            "checks": {"gateway": True},
        }
        with patch.object(boot_verifier, "DB_PATH", self.db):
            boot_verifier.write_status(result, 3, "ok")

        with sqlite3.connect(self.db) as conn:
            values = dict(conn.execute(
                "SELECT key, value FROM runtime_status WHERE key LIKE 'boot_verify_%'"
            ).fetchall())
        self.assertEqual(values["boot_verify_state"], "ok")
        self.assertEqual(values["boot_verify_attempts"], "3")
        self.assertEqual(values["boot_verify_end_to_end_ready"], "1")
        self.assertIn('"gateway":true', values["boot_verify_detail_json"])


if __name__ == "__main__":
    unittest.main()
