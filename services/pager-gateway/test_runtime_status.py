from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage import Storage


class RuntimeStatusTests(unittest.TestCase):
    def test_runtime_status_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "pager.db"))
            storage.update_runtime_status({
                "pdl_service": "active",
                "audio_capture_devices": 1,
                "cpu_temp_c": "42.5",
            })

            status = storage.get_runtime_status()
            self.assertEqual(status["pdl_service"]["value"], "active")
            self.assertEqual(status["audio_capture_devices"]["value"], "1")
            self.assertEqual(status["cpu_temp_c"]["value"], "42.5")
            self.assertTrue(status["pdl_service"]["updated_at"])

    def test_runtime_status_upsert_replaces_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "pager.db"))
            storage.update_runtime_status({"pdl_service": "inactive"})
            storage.update_runtime_status({"pdl_service": "active"})
            status = storage.get_runtime_status()
            self.assertEqual(status["pdl_service"]["value"], "active")


if __name__ == "__main__":
    unittest.main()
