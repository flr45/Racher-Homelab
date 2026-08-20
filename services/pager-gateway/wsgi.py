from __future__ import annotations

import hashlib
import re
from pathlib import Path

from gateway import FileTailSource


# Hold the live PDL tailer until the operations, notification policy and RSS layers
# have installed their tracking/routes. This removes startup races where the first
# alarm after a container restart could otherwise be delivered before policy is ready.
_real_file_tail_start = FileTailSource.start
FileTailSource.start = lambda self: None
try:
    import app as app_module
    import app_core as core
finally:
    FileTailSource.start = _real_file_tail_start

from alarm_rules import install_alarm_rules
from burst_consensus import install_burst_consensus
from operations import install_operations
from pdl_multiline import install_pdl_multiline_tail
from pushover_destinations import install_pushover_destinations
from ric_sms_remote import install_ric_sms
from rss_updates import install_rss_updates


# Pushover destination management must wrap the core sender before alarm_rules
# adds its alarm-time decoration. Burst consensus wraps the final ingest path.
# Operations wraps the Pushover channel first; RIC SMS is intentionally installed
# afterwards so the SMS trigger remains independent of whether Pushover is enabled.
# This also lets one multi-RIC burst create only one SMS per configured phone.
pushover_destinations = install_pushover_destinations(core)
alarm_rules = install_alarm_rules(core)
burst_consensus = install_burst_consensus(core, alarm_rules)
operations = install_operations(core)
ric_sms = install_ric_sms(core, core.auth_required)
rss_updates = install_rss_updates(core)
install_pdl_multiline_tail(core.source)
core.source.start()
rss_updates.start()
app = app_module.app


# Browsers may keep an older copy of CSS/JavaScript even though the HTML itself is
# deliberately no-store. Build a deterministic version from the actual static files
# and append it to every CSS/JS asset referenced by HTML.
_STATIC_ROOT = Path(app.static_folder or "/app/static")
_STATIC_ASSET_RE = re.compile(r'(/static/[A-Za-z0-9._-]+\.(?:css|js))(?!\?v=)')
_ADAPTIVE_CARD_RE = re.compile(
    r'(<article class="card"><span class="label">Adaptiv filtrering</span>.*?</article>)',
    re.DOTALL,
)
_ALARM_MAP_SCRIPT = '<script src="/static/alarm-map.js" defer></script>'
_ALARM_FILTER_SCRIPT = '<script src="/static/alarm-filter-ui.js" defer></script>'
_PUSHOVER_ADMIN_SCRIPT = '<script src="/static/pushover-admin.js" defer></script>'
_RIC_SMS_ADMIN_SCRIPT = '<script src="/static/ric-sms-admin.js" defer></script>'
_ALARM_FILTER_CARD = """
          <article class="card" id="alarm-filter-card">
            <span class="label">Manuelt alarmfilter</span>
            <h2>Filtrer alarmord</h2>
            <p class="hint">Hvis en pageralarm indeholder et af ordene eller fraserne herunder, gemmes råmeldingen stadig i adminhistorikken, men den vises ikke i Alarmfeed og sendes ikke som Web Push eller Pushover.</p>
            <div class="form-grid">
              <label class="wide">Ord eller fraser
                <input id="alarm-filter-terms" type="text" maxlength="4000" autocomplete="off" placeholder="fx TEST, ØVELSE, servicebesked">
              </label>
            </div>
            <p class="hint">Adskil flere filtre med komma eller semikolon. Store og små bogstaver behandles ens.</p>
            <div class="actions">
              <button id="save-alarm-filters" class="primary" type="button">Gem alarmfilter</button>
              <span id="alarm-filter-status" class="muted">Henter…</span>
            </div>
          </article>""".strip()
_PUSHOVER_HINT = '<p class="hint">Tilføjede Pushover-modtagere vises nedenfor med navn og maskeret user/group key. RIC sendes aldrig med, og støj/dubletter undertrykkes også her.</p>'
_PUSHOVER_MANAGER = """
          <div id="pushover-destination-manager" class="split-section">
            <div>
              <h3>Pushover-modtagere</h3>
              <p class="hint">Her kan du se de Pushover user/group keys, der er tilføjet. Nøgler vises maskeret efter de er gemt.</p>
              <div id="pushover-destination-list" class="command-list"><p class="muted">Henter modtagere…</p></div>
            </div>
            <div>
              <h3>Tilføj modtager</h3>
              <div class="form-grid compact-form">
                <label>Navn<input id="pushover-destination-label" maxlength="80" autocomplete="off" placeholder="fx Frederik"></label>
                <label>User/group key<input id="pushover-destination-key" type="password" minlength="20" maxlength="80" autocomplete="off" placeholder="Pushover user/group key"></label>
              </div>
              <div class="actions"><button id="pushover-destination-add" type="button" class="primary">Tilføj modtager</button><span id="pushover-destination-status" class="muted">Henter…</span></div>
            </div>
          </div>""".strip()


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


def _enhance_home_html(body: str) -> str:
    # The manual filter is server-rendered in the template. Keep this fallback so
    # older/custom templates still expose it after an appliance update.
    if 'id="alarm-filter-card"' not in body:
        body, _ = _ADAPTIVE_CARD_RE.subn(
            lambda match: f"{match.group(1)}\n{_ALARM_FILTER_CARD}",
            body,
            count=1,
        )

    # Render the Pushover destination manager in the response as well. JavaScript
    # only fills data and handles actions; the controls remain visibly present if a
    # helper script fails, which makes configuration failures diagnosable.
    if 'id="pushover-destination-manager"' not in body and _PUSHOVER_HINT in body:
        body = body.replace(_PUSHOVER_HINT, f"{_PUSHOVER_HINT}\n{_PUSHOVER_MANAGER}", 1)

    if "</body>" in body:
        helpers: list[str] = []
        is_admin_page = 'data-admin="1"' in body
        if is_admin_page and "alarm-filter-ui.js" not in body:
            helpers.append(_ALARM_FILTER_SCRIPT)
        if is_admin_page and "pushover-admin.js" not in body:
            helpers.append(_PUSHOVER_ADMIN_SCRIPT)
        if is_admin_page and "ric-sms-admin.js" not in body:
            helpers.append(_RIC_SMS_ADMIN_SCRIPT)
        if "alarm-map.js" not in body:
            helpers.append(_ALARM_MAP_SCRIPT)
        if helpers:
            body = body.replace("</body>", "  " + "\n  ".join(helpers) + "\n</body>")
    return body


@app.after_request
def version_static_assets(response):
    if core.request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        response.headers["X-Pager-Frontend-Version"] = STATIC_ASSET_VERSION
        return response

    content_type = str(response.headers.get("Content-Type") or "")
    if response.status_code == 200 and content_type.startswith("text/html"):
        body = response.get_data(as_text=True)
        if core.request.path == "/":
            body = _enhance_home_html(body)
        body = _STATIC_ASSET_RE.sub(rf"\1?v={STATIC_ASSET_VERSION}", body)
        response.set_data(body)
        response.headers["Content-Length"] = str(len(response.get_data()))
        response.headers["X-Pager-Frontend-Version"] = STATIC_ASSET_VERSION
    return response