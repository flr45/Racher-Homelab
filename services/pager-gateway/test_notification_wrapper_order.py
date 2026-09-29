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

    def test_whatsapp_wraps_final_notification_layer(self):
        source = (Path(__file__).with_name("wsgi.py")).read_text(encoding="utf-8")
        operations = source.index("operations = install_operations(core)")
        ric_sms = source.index("ric_sms = install_ric_sms(core, core.auth_required)")
        whatsapp = source.index("whatsapp = install_whatsapp(")
        self.assertLess(operations, ric_sms)
        self.assertLess(
            ric_sms,
            whatsapp,
            "WhatsApp must wrap the final notification hook so burst-consensus alarms are delivered",
        )

    def test_training_routes_do_not_install_whatsapp_early(self):
        source = (Path(__file__).with_name("training_routes.py")).read_text(encoding="utf-8")
        self.assertNotIn(
            "install_whatsapp(",
            source,
            "WhatsApp must not be installed before wsgi notification wrappers replace the sender",
        )


if __name__ == "__main__":
    unittest.main()
