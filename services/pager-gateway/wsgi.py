from __future__ import annotations

import app as app_module
import app_core as core

from operations import install_operations


# app.py owns authentication, recovery and source-mode hardening. The operations
# layer is installed after that composition is complete so it can add telemetry
# without duplicating the core gateway implementation.
operations = install_operations(core)
app = app_module.app
