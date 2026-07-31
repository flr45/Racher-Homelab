"""Runtime compatibility tweaks for Huawei USB serial modems.

Some Huawei modem/driver combinations reject the TIOCMBIS ioctl that pySerial
uses to assert DTR when a port is reopened. That surfaces as BrokenPipeError
before any AT command is sent. Keep DTR under modem/driver control by default.
"""

import os

import serial


if os.getenv("MODEM_DISABLE_DTR_TOGGLE", "true").lower() == "true":
    _OriginalSerial = serial.Serial

    class HuaweiCompatibleSerial(_OriginalSerial):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("dsrdtr", True)
            super().__init__(*args, **kwargs)

    serial.Serial = HuaweiCompatibleSerial
