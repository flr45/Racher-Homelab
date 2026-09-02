from __future__ import annotations

import unittest
from pathlib import Path


class NotificationWrapperOrderTests(unittest.TestCase):
    def test_ric_sms_wraps_operations_pushover_layer(self):
        source = (Path(__file__).with_name("wsgi.py")).read_text(encoding="utf-8")
        operations = source.index("operations = install_operations(core)")
        ric_sms = source.index("ric_sms = install_ric_sms(core, core.auth_required)")
        self.assertLess(
            operations,
            ric_sms,
            "RIC SMS must be installed after operations so Pushover-disabled alarms can still trigger SMS",
        )


if __name__ == "__main__":
    unittest.main()
