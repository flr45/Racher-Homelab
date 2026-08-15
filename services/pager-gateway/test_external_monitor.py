import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import external_monitor as monitor


class ExternalMonitorTests(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            monitor.write_state({"enabled": True, "failure_count": 2}, path)
            self.assertEqual(monitor.read_state(path)["failure_count"], 2)

    def test_three_failures_send_one_alarm_and_cache_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            monitor.write_state(
                {
                    "enabled": True,
                    "sms_to": "+4512345678",
                    "failure_threshold": 3,
                    "failure_count": 2,
                    "alarm_active": False,
                    "gateway_name": "Pager",
                },
                state_file,
            )
            with patch.object(monitor, "STATE_FILE", state_file), \
                 patch.object(monitor, "health_ok", return_value=False), \
                 patch.object(monitor, "tailscale_reachable", return_value=False), \
                 patch.object(monitor, "send_sms", return_value=True) as send:
                self.assertEqual(monitor.main(), 0)
            updated = monitor.read_state(state_file)
            self.assertTrue(updated["alarm_active"])
            self.assertEqual(updated["failure_count"], 3)
            send.assert_called_once()
            self.assertIn("Pi/netvaerk", send.call_args.args[1])

            with patch.object(monitor, "STATE_FILE", state_file), \
                 patch.object(monitor, "health_ok", return_value=False), \
                 patch.object(monitor, "tailscale_reachable", return_value=False), \
                 patch.object(monitor, "send_sms", return_value=True) as second_send:
                self.assertEqual(monitor.main(), 0)
            second_send.assert_not_called()

    def test_recovery_sends_one_sms_and_clears_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            monitor.write_state(
                {
                    "enabled": True,
                    "sms_to": "+4512345678",
                    "failure_threshold": 3,
                    "failure_count": 4,
                    "alarm_active": True,
                    "gateway_name": "Pager",
                    "outage_started_at": "2026-08-15T06:00:00+00:00",
                },
                state_file,
            )
            config = {
                "ok": True,
                "enabled": True,
                "sms_to": "+4512345678",
                "failure_threshold": 3,
                "gateway_name": "Pager",
            }
            with patch.object(monitor, "STATE_FILE", state_file), \
                 patch.object(monitor, "health_ok", return_value=True), \
                 patch.object(monitor, "fetch_config", return_value=config), \
                 patch.object(monitor, "send_sms", return_value=True) as send:
                self.assertEqual(monitor.main(), 0)
            updated = monitor.read_state(state_file)
            self.assertFalse(updated["alarm_active"])
            self.assertEqual(updated["failure_count"], 0)
            send.assert_called_once()
            self.assertIn("online igen", send.call_args.args[1])

    def test_disabled_monitor_never_sends_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            monitor.write_state(
                {"enabled": False, "sms_to": "+4512345678", "failure_threshold": 1},
                state_file,
            )
            with patch.object(monitor, "STATE_FILE", state_file), \
                 patch.object(monitor, "health_ok", return_value=False), \
                 patch.object(monitor, "tailscale_reachable", return_value=True), \
                 patch.object(monitor, "send_sms", return_value=True) as send:
                self.assertEqual(monitor.main(), 0)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
