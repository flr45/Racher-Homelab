from __future__ import annotations

import hashlib
import re
from pathlib import Path

from gateway import FileTailSource


# Hold the live PDL tailer until the operations, alarm rules and RSS layers have
# installed their tracking/routes. This removes startup races where the first alarm
# after a container restart could otherwise be delivered before policy is ready.
_real_file_tail_start = FileTailSource.start
FileTailSource.start = lambda self: None
try:
    import app as app_module
    import app_core as core
finally:
    FileTailSource.start = _real_file_tail_start

from alarm_rules import install_alarm_rules
from operations import install_operations
from rss_updates import install_rss_updates


alarm_rules = install_alarm_rules(core)
operations = install_operations(core)
rss_updates = install_rss_updates(core)
core.source.start()
rss_updates.start()
app = app_module.app


# Browsers may keep an older copy of CSS/JavaScript even though the HTML itself is
# deliberately no-store. That made UI deployments look unchanged until the local
# browser cache was manually cleared. Build a deterministic version from the
# actual static files and append it to every CSS/JS asset referenced by HTML.
# The URL therefore changes automatically whenever any frontend asset changes.
_STATIC_ROOT = Path(app.static_folder or "/app/static")
_STATIC_ASSET_RE = re.compile(r'(/static/[A-Za-z0-9._-]+\.(?:css|js))(?!\?v=)')
_ALARM_MAP_SCRIPT = '<script src="/static/alarm-map.js" defer></script>'


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(_STATIC_ROOT.glob("*")):
        if path.is_file() and path.suffix.lower() in {".css", ".js"}:
            digest.update(path.name.encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
    return digest.hexdigest()[:12]


STATIC_ASSET_VERSION = _static_asset_version()


@app.after_request
def version_static_assets(response):
    # New static responses must always revalidate. The query-string fingerprint
    # below handles clients that still possess a previously fresh cached object.
    if core.request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        return response

    content_type = str(response.headers.get("Content-Type") or "")
    if response.status_code == 200 and content_type.startswith("text/html"):
        body = response.get_data(as_text=True)
        # The map helper is deliberately kept isolated from the core alarm UI. It
        # only enhances the authenticated home page and can therefore evolve
        # without touching alarm ingestion/routing code.
        if core.request.path == "/" and "alarm-map.js" not in body and "</body>" in body:
            body = body.replace("</body>", f"  {_ALARM_MAP_SCRIPT}\n</body>")
        body = _STATIC_ASSET_RE.sub(rf"\1?v={STATIC_ASSET_VERSION}", body)
        response.set_data(body)
        response.headers["Content-Length"] = str(len(response.get_data()))
    return response
