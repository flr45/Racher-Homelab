from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


class FrontendPollingTests(unittest.TestCase):
    def test_main_pollers_only_run_for_visible_active_panels(self):
        source = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("document.visibilityState === 'visible'", source)
        self.assertIn("panelIsActive('alarms')", source)
        self.assertIn("panelIsActive('system')", source)
        self.assertIn("visibilitychange", source)

    def test_system_overview_reuses_main_status_response(self):
        app_source = (STATIC / "app.js").read_text(encoding="utf-8")
        overview_source = (STATIC / "system-overview.js").read_text(encoding="utf-8")
        self.assertIn("new CustomEvent('pager:status'", app_source)
        self.assertIn("addEventListener('pager:status'", overview_source)
        self.assertNotIn("fetch('/api/status'", overview_source)
        self.assertNotIn("setInterval(refresh, 10000)", overview_source)


if __name__ == "__main__":
    unittest.main()
