from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["SYSTEM_LINK_SECRET"] = "test-legacy-secret"
os.environ["SYSTEM_LINK_MASTER_SECRET"] = "test-master-secret"
os.environ["SYSTEM_LINK_PROVISION_TOKEN"] = "one-time-token"
os.environ["SYSTEM_LINK_PUBLIC_ENDPOINT"] = "https://link.example.test/api/system-link"

import app as receiver


class SystemLinkReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["SYSTEM_LINK_DB"] = str(Path(self.temp.name) / "system-link.db")
        receiver.init_database()
        self.client = receiver.app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _signed_body(
        payload: dict,
        secret: str,
        client_id: str | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-System-Link-Signature": f"sha256={digest}",
        }
        if client_id:
            headers["X-System-Link-Client"] = client_id
        return raw, headers

    def _provision(self):
        return self.client.post(
            "/api/provision",
            json={
                "token": "one-time-token",
                "machineName": "SBR-PC-01",
                "appVersion": "0.1-test",
            },
        )

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["provisioning"])

    def test_existing_database_is_migrated_before_client_index(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        os.environ["SYSTEM_LINK_DB"] = str(legacy_path)
        db = sqlite3.connect(legacy_path)
        try:
            db.executescript(
                """
                CREATE TABLE deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    receiver_received_at TEXT NOT NULL,
                    station TEXT
                );
                """
            )
            db.commit()
        finally:
            db.close()

        receiver.init_database()
        with receiver.connection() as migrated:
            columns = {
                str(row[1])
                for row in migrated.execute("PRAGMA table_info(deliveries)").fetchall()
            }
            indexes = {
                str(row[1])
                for row in migrated.execute("PRAGMA index_list(deliveries)").fetchall()
            }
        self.assertIn("client_id", columns)
        self.assertIn("idx_deliveries_client", indexes)

    def test_provisioning_token_is_one_time(self) -> None:
        first = self._provision()
        self.assertEqual(first.status_code, 201)
        credentials = first.get_json()
        self.assertTrue(credentials["clientId"].startswith("gw-"))
        self.assertTrue(credentials["clientSecret"])
        self.assertEqual(
            credentials["messageEndpoint"],
            "https://link.example.test/api/system-link",
        )

        second = self._provision()
        self.assertEqual(second.status_code, 409)
        self.assertIn("allerede brugt", second.get_json()["error"])

    def test_unsigned_request_is_rejected(self) -> None:
        response = self.client.post(
            "/api/system-link",
            data=b'{"schema":"sbr-pager-gateway.system-link.v1","test":true}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_provisioned_client_can_sign_requests(self) -> None:
        credentials = self._provision().get_json()
        raw, headers = self._signed_body(
            {
                "schema": "sbr-pager-gateway.system-link.v1",
                "gateway": "SBR Pager Gateway",
                "test": True,
            },
            credentials["clientSecret"],
            credentials["clientId"],
        )
        response = self.client.post("/api/system-link", data=raw, headers=headers)
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["clientId"], credentials["clientId"])

    def test_legacy_signed_request_is_still_accepted(self) -> None:
        raw, headers = self._signed_body(
            {
                "schema": "sbr-pager-gateway.system-link.v1",
                "gateway": "SBR Pager Gateway",
                "test": True,
            },
            "test-legacy-secret",
        )
        response = self.client.post("/api/system-link", data=raw, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_alarm_is_stored_once_and_duplicate_is_idempotent(self) -> None:
        credentials = self._provision().get_json()
        payload = {
            "schema": "sbr-pager-gateway.system-link.v1",
            "gateway": "SBR Pager Gateway",
            "deliveryId": "sbr-system-link:123",
            "message": {
                "id": 123,
                "sourceId": "source-123",
                "sender": "+4512345678",
                "body": "Alarm (S) · Bygningsbrand · Testvej 1, 4200 Slagelse",
                "receivedAt": "2026-09-17T17:00:00Z",
                "createdAt": "2026-09-17T17:00:01Z",
                "modemPort": "COM5",
                "processingStatus": "accepted_pending_whatsapp",
            },
            "event": {
                "id": 77,
                "station": "S",
                "alarmType": "Bygningsbrand",
                "address": "Testvej 1, 4200 Slagelse",
                "status": "complete",
                "followupCount": 0,
                "startedAt": "2026-09-17T17:00:00Z",
                "completedAt": "2026-09-17T17:00:02Z",
            },
        }
        raw, headers = self._signed_body(
            payload,
            credentials["clientSecret"],
            credentials["clientId"],
        )

        first = self.client.post("/api/system-link", data=raw, headers=headers)
        second = self.client.post("/api/system-link", data=raw, headers=headers)

        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.get_json()["duplicate"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])

        with receiver.connection() as db:
            rows = db.execute("SELECT * FROM deliveries").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["station"], "S")
        self.assertEqual(rows[0]["alarm_type"], "Bygningsbrand")
        self.assertEqual(rows[0]["client_id"], credentials["clientId"])


if __name__ == "__main__":
    unittest.main()
