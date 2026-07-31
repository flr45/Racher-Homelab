"""Huawei USB serial compatibility loaded automatically by Python.

Some Huawei modem/driver combinations reject the Linux ioctls used by pySerial
to assert DTR or RTS when a USB serial port is opened again. The data channel
still works, so only ignore EPIPE for those two optional control-line updates.
"""

import errno
import logging
import os

import serial

log = logging.getLogger("sms-modem-serial")


if os.getenv("MODEM_DISABLE_DTR_TOGGLE", "true").lower() == "true":
    _OriginalSerial = serial.Serial

    class HuaweiCompatibleSerial(_OriginalSerial):
        def _update_dtr_state(self):
            try:
                super()._update_dtr_state()
            except OSError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                log.warning("Modemdriver afviste DTR-ioctl; fortsætter uden DTR-skift")

        def _update_rts_state(self):
            try:
                super()._update_rts_state()
            except OSError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                log.warning("Modemdriver afviste RTS-ioctl; fortsætter uden RTS-skift")

    serial.Serial = HuaweiCompatibleSerial
