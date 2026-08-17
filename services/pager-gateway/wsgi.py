from __future__ import annotations

from gateway import FileTailSource


# Hold the live PDL tailer until the operations layer has installed delivery
# tracking. This removes the tiny startup race where the first alarm after a
# container restart could otherwise be delivered correctly but miss telemetry.
_real_file_tail_start = FileTailSource.start
FileTailSource.start = lambda self: None
try:
    import app as app_module
    import app_core as core
finally:
    FileTailSource.start = _real_file_tail_start

from operations import install_operations


operations = install_operations(core)
core.source.start()
app = app_module.app
