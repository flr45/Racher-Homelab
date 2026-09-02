from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from system_overview import SystemOverview


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({
            "status": "ok",
            "checked_at": "2026-08-21T14:00:00+00:00",
            "modem": {
                "state": "online",
                "signal": "+CSQ: 23,99",
                "network": "+CREG: 0,5",
            },
        }).encode("utf-8")


class SystemOverviewTests(unittest.TestCase):
    @staticmethod
    def _runtime():
        return {
            "agent_heartbeat": datetime.now(timezone.utc).isoformat(),
            "fsk_usb_connected": "1",
            "fsk_usb_pdl_in_use": "1",
            "pdl_service": "active",
            "gateway_container": "running",
            "internet_online": "1",
            "host_uptime_seconds": "3600",
        }

    def test_complete_chain_is_green_when_remote_sms_and_modem_are_online(self):
        overview = SystemOverview(SimpleNamespace())
        with patch.dict(os.environ, {"PAGER_SMS_GATEWAY_URL": "http://100.111.28.12:8090"}, clear=False):
            with patch("system_overview.urllib.request.urlopen", return_value=_Response()):
                result = overview.snapshot(self._runtime())

        self.assertTrue(result["local_ready"])
        self.assertTrue(result["end_to_end_ready"])
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["sms"]["endpoint"], "100.111.28.12:8090")
        self.assertEqual(result["sms"]["modem_state"], "online")
        states = {item["key"]: item["state"] for item in result["chain"]}
        self.assertEqual(states["fsk"], "ok")
        self.assertEqual(states["pdl"], "ok")
        self.assertEqual(states["sms-link"], "ok")
        self.assertEqual(states["gsm"], "ok")

    def test_local_pager_can_be_ready_when_sms_gateway_is_not_configured(self):
        overview = SystemOverview(SimpleNamespace())
        with patch.dict(os.environ, {"PAGER_SMS_GATEWAY_URL": ""}, clear=False):
            result = overview.snapshot(self._runtime())

        self.assertTrue(result["local_ready"])
        self.assertFalse(result["end_to_end_ready"])
        self.assertEqual(result["state"], "local-ok")
        self.assertFalse(result["sms"]["configured"])


if __name__ == "__main__":
    unittest.main()
