from __future__ import annotations

import unittest
from pathlib import Path


class AdminTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parent
        cls.html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        cls.app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        cls.overview_js = (root / "static" / "system-overview.js").read_text(encoding="utf-8")
        cls.rss_js = (root / "static" / "rss-updates.js").read_text(encoding="utf-8")

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

    def test_background_polling_is_visibility_and_tab_aware(self):
        self.assertIn("activePanelName() === 'alarms'", self.app_js)
        self.assertIn("activePanelName() === 'system'", self.app_js)
        self.assertIn("document.addEventListener('visibilitychange'", self.app_js)
        self.assertIn("!document.hidden", self.rss_js)

    def test_system_overview_reuses_shared_status_poll(self):
        self.assertIn("window.addEventListener('pager:status'", self.overview_js)
        self.assertNotIn("fetch('/api/status'", self.overview_js)
        self.assertNotIn("setInterval(refresh, 10000)", self.overview_js)

    def test_alarm_rows_render_delivery_badges(self):
        self.assertIn("function deliveryBadges(row)", self.app_js)
        self.assertIn("deliveryChannelLabels", self.app_js)
        self.assertIn("Pushover", self.app_js)
        self.assertIn("Web Push", self.app_js)
        self.assertIn("delivery-strip", self.app_js)

    def test_combined_1200_2400_decoder_option_is_visible(self):
        self.assertIn('<option value="1200+2400">1200 + 2400</option>', self.html)
        self.assertIn("deaktiverer 512 i PDL", self.html)


if __name__ == "__main__":
    unittest.main()
