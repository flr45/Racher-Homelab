from __future__ import annotations

from gateway import FileTailSource


# Hold the live PDL tailer until the operations and RSS layers have installed
# their tracking/routes. This removes startup races where the first alarm after a
# container restart could otherwise be delivered before telemetry is ready.
_real_file_tail_start = FileTailSource.start
FileTailSource.start = lambda self: None
try:
    import app as app_module
    import app_core as core
finally:
    FileTailSource.start = _real_file_tail_start

from operations import install_operations
from rss_updates import install_rss_updates


operations = install_operations(core)
rss_updates = install_rss_updates(core)
core.source.start()
rss_updates.start()
app = app_module.app
