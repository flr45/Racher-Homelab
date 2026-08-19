from __future__ import annotations

import unittest
from pathlib import Path


class AdminTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")

    def test_manual_alarm_filter_is_server_rendered(self):
        self.assertIn('id="alarm-filter-card"', self.html)
        self.assertIn('id="alarm-filter-terms"', self.html)
        self.assertIn('id="save-alarm-filters"', self.html)
        self.assertIn("Manuelt alarmfilter", self.html)
        self.assertIn("Filtrer alarmord", self.html)

    def test_admin_helpers_are_loaded_directly(self):
        self.assertIn('/static/alarm-filter-ui.js', self.html)
        self.assertIn('/static/pushover-admin.js', self.html)
        self.assertIn('/static/alarm-map.js', self.html)

    def test_legacy_pushover_key_is_not_visible(self):
        self.assertNotIn('label>User/group key<input type="password" name="pushover_user_key"', self.html)
        self.assertIn('type="hidden" name="pushover_user_key"', self.html)
        self.assertIn("Tilføjede Pushover-modtagere", self.html)

    def test_combined_1200_2400_decoder_option_is_visible(self):
        self.assertIn('<option value="1200+2400">1200 + 2400</option>', self.html)
        self.assertIn("deaktiverer 512 i PDL", self.html)


if __name__ == "__main__":
    unittest.main()
