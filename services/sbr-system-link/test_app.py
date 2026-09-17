from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["SYSTEM_LINK_SECRET"] = "test-secret"

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
    def _signed_body(payload: dict) -> tuple[bytes, dict[str, str]]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        return raw, {
            "Content-Type": "application/json; charset=utf-8",
            "X-System-Link-Signature": f"sha256={digest}",
        }

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_unsigned_request_is_rejected(self) -> None:
        response = self.client.post(
            "/api/system-link",
            data=b'{"schema":"sbr-pager-gateway.system-link.v1","test":true}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_signed_test_request_is_accepted(self) -> None:
        raw, headers = self._signed_body(
            {
                "schema": "sbr-pager-gateway.system-link.v1",
                "gateway": "SBR Pager Gateway",
                "test": True,
            }
        )
        response = self.client.post("/api/system-link", data=raw, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_alarm_is_stored_once_and_duplicate_is_idempotent(self) -> None:
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
        raw, headers = self._signed_body(payload)

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


if __name__ == "__main__":
    unittest.main()
