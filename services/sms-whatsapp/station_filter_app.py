"""Station subscriptions for SBR Pager WhatsApp recipients.

Recipients can subscribe to A/S/L/K/R/B, TEST or ALL. TEST is always explicit
opt-in and is never included by ALL. Existing recipients default to ALL until a
filter is explicitly saved. Sending 2 without a station marker is
linked to the most recent alarm event from the same sender so follow-ups keep
the same station routing.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta

from flask import flash, jsonify, redirect, render_template_string, request, url_for

import stats_v2 as previous

app = previous.app
events = previous.events
base = previous.base
db = base.db

NORMAL_STATIONS = ("A", "S", "L", "K", "R", "B")
TEST_STATION = "TEST"
STATIONS = NORMAL_STATIONS + (TEST_STATION,)
ALL_STATIONS = "*"
_STATION_RE = re.compile(r"\(([ASLKRB])\)", re.IGNORECASE)
_TEST_RE = re.compile(r"(?<!\w)test(?!\w)", re.IGNORECASE)
_SENDING_2_RE = re.compile(r"\bsending\s*2\b", re.IGNORECASE)
PREALERT_DELAY_KEY = "prealert_delay_seconds"


class RecipientStationFilter(db.Model):
    __tablename__ = "recipient_station_filter"
    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(
        db.Integer,
        db.ForeignKey("recipient.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    station = db.Column(db.String(4), nullable=False, index=True)
    __table_args__ = (
        db.UniqueConstraint("recipient_id", "station", name="uq_recipient_station"),
    )


class PagerRuntimeSetting(db.Model):
    __tablename__ = "pager_runtime_setting"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), nullable=False, unique=True, index=True)
    value = db.Column(db.String(255), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=base.utcnow,
        onupdate=base.utcnow,
    )


def _default_prealert_delay_seconds() -> float:
    raw = os.getenv(
        "SBR_PAGER_PREALERT_DELAY_SECONDS",
        os.getenv("SMS_WHATSAPP_PREALERT_DELAY_SECONDS", "10"),
    )
    try:
        return max(0.0, min(120.0, float(raw)))
    except (TypeError, ValueError):
        return 10.0


def prealert_delay_seconds() -> float:
    row = PagerRuntimeSetting.query.filter_by(key=PREALERT_DELAY_KEY).first()
    if row is None:
        return _default_prealert_delay_seconds()
    try:
        return max(0.0, min(120.0, float(row.value)))
    except (TypeError, ValueError):
        return _default_prealert_delay_seconds()


def format_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def selection_label(selected: set[str]) -> str:
    if ALL_STATIONS in selected:
        return "Alle stationer + Test" if TEST_STATION in selected else "Alle stationer"
    labels = ["Test" if value == TEST_STATION else value for value in sorted(selected)]
    return ", ".join(labels) if labels else "Alle stationer"


def detect_station(text: str | None) -> str | None:
    value = text or ""
    # TEST har altid forrang. En testbesked må aldrig ramme modtagere, der
    # kun har valgt "Alle stationer".
    if _TEST_RE.search(value):
        return TEST_STATION
    match = _STATION_RE.search(value)
    return match.group(1).upper() if match else None


def configured_stations(recipient_id: int) -> set[str]:
    rows = RecipientStationFilter.query.filter_by(recipient_id=recipient_id).all()
    if not rows:
        # Backwards compatible: recipients created before this feature get all.
        return {ALL_STATIONS}
    values = {row.station.upper() for row in rows}
    return values or {ALL_STATIONS}


def recipient_accepts(recipient_id: int, station: str | None) -> bool:
    selected = configured_stations(recipient_id)
    normalized = station.upper() if station else None

    # TEST er opt-in. "*" dækker kun de almindelige stationer.
    if normalized == TEST_STATION:
        return TEST_STATION in selected

    if ALL_STATIONS in selected:
        return True
    return normalized is not None and normalized in selected


def station_for_inbound(inbound: base.InboundMessage) -> str | None:
    station = detect_station(inbound.body)
    if station:
        return station

    # The modem gateway supplies the exact parent event key for Sending 2.
    # Use it before the time-based fallback so recipient routing matches the
    # original alarm even when several incidents are active close together.
    try:
        payload = request.get_json(silent=True) or {}
    except RuntimeError:
        payload = {}
    parent_event_key = str(payload.get("parentEventKey") or "").strip()
    if parent_event_key:
        parent = events.AlarmEvent.query.filter_by(event_key=parent_event_key[:128]).first()
        if parent and parent.station:
            return parent.station.upper()

    # Sending 2 often contains no (S)/(A)/... marker. Reuse the station from the
    # most recent alarm event from the same sender inside the event-link window.
    if not _SENDING_2_RE.search(inbound.body or ""):
        return None

    received = events.aware(inbound.received_at) or base.utcnow()
    lower = received - timedelta(minutes=events.EVENT_LINK_MINUTES)
    parent = (
        events.AlarmEvent.query.filter(
            events.AlarmEvent.sender == inbound.sender,
            events.AlarmEvent.started_at >= lower,
            events.AlarmEvent.started_at <= received + timedelta(minutes=5),
        )
        .order_by(events.AlarmEvent.started_at.desc())
        .first()
    )
    return parent.station.upper() if parent and parent.station else None


def deliver_inbound_filtered(inbound: base.InboundMessage) -> tuple[int, int]:
    station = station_for_inbound(inbound)
    recipients = base.Recipient.query.filter_by(active=True).order_by(base.Recipient.name).all()
    sent = 0
    failed = 0

    for recipient in recipients:
        if not recipient_accepts(recipient.id, station):
            continue

        delivery = base.WhatsAppDelivery(
            inbound_id=inbound.id,
            recipient_name=recipient.name,
            recipient_phone=recipient.phone,
            status="pending",
            attempted_at=base.utcnow(),
        )
        db.session.add(delivery)
        db.session.commit()
        try:
            delivery.message_id = base.send_whatsapp(recipient.phone, inbound.body)
            delivery.status = "sent"
            delivery.error = None
            sent += 1
        except Exception as exc:  # noqa: BLE001
            delivery.status = "failed"
            delivery.error = str(exc)[:1000]
            failed += 1
        delivery.attempted_at = base.utcnow()
        db.session.commit()

    return sent, failed


# app.py's incoming() resolves deliver_inbound from its module globals at call
# time, so replacing it here also affects the already wrapped events endpoint.
base.deliver_inbound = deliver_inbound_filtered


STATION_FILTER_FRAGMENT = r"""
<style>
.station-grid{display:grid;grid-template-columns:repeat(8,minmax(48px,1fr)) auto;gap:8px;align-items:center}
.station-choice{display:flex;align-items:center;justify-content:center;gap:5px;background:#0d141e;border:1px solid var(--border);border-radius:10px;padding:8px;cursor:pointer}
.station-choice input{width:auto;margin:0}.recipient-filter{padding:12px 0;border-bottom:1px solid #213044}.recipient-filter:last-child{border-bottom:0}.recipient-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:9px}
@media(max-width:880px){.station-grid{grid-template-columns:repeat(4,1fr)}.station-grid .btn{grid-column:span 4}}
</style>
<section class="card span12" id="stationsfilter">
  <div class="top" style="margin-bottom:10px"><div><h2 style="margin:0">Stationsfilter</h2><p class="muted" style="margin:5px 0 0">Vælg hvilke stationer hver WhatsApp-modtager får. “Alle” dækker normale alarmer; Test skal vælges aktivt.</p></div><a class="btn small" href="{{ url_for('station_filters_page') }}">Åbn separat</a></div>
  {% for item in station_recipients %}
  <form class="recipient-filter" method="post" action="{{ url_for('update_recipient_stations', recipient_id=item.recipient.id) }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="recipient-head"><strong>{{ item.recipient.name }}</strong><span class="muted">{{ item.recipient.phone }}</span></div>
    <div class="station-grid">
      <label class="station-choice"><input type="checkbox" name="stations" value="*" {{ 'checked' if '*' in item.selected else '' }}>Alle</label>
      {% for code in stations %}<label class="station-choice"><input type="checkbox" name="stations" value="{{ code }}" {{ 'checked' if code in item.selected else '' }}>{{ 'Test' if code == 'TEST' else code }}</label>{% endfor %}
      <button class="btn primary small" type="submit">Gem filter</button>
    </div>
  </form>
  {% else %}<div class="empty">Ingen WhatsApp-modtagere endnu.</div>{% endfor %}
</section>
"""



PREALERT_SETTING_FRAGMENT = r"""
<section class="card span12" id="prealert-setting">
  <div class="top" style="margin-bottom:10px">
    <div>
      <h2 style="margin:0">Pre-alarm delay</h2>
      <p class="muted" style="margin:5px 0 0">Ventetid før første del af en ufuldstændig multipart-alarm sendes. Ændringen bruges automatisk af SMS Gateway uden genstart.</p>
    </div>
    <form method="post" action="{{ url_for('update_prealert_delay') }}" style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div><label class="muted">Sekunder</label><input name="seconds" type="number" min="0" max="120" step="0.5" value="{{ prealert_delay }}" style="width:110px"></div>
      <button class="btn primary" type="submit">Gem delay</button>
    </form>
  </div>
  <div class="muted">0 sek. = pre-alarm med det samme. Hvis resten af SMS'en når frem inden tiden udløber, sendes kun den komplette alarm.</div>
</section>
"""

STATION_FILTER_PAGE = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<style>
.station-grid{display:grid;grid-template-columns:repeat(8,minmax(48px,1fr)) auto;gap:8px;align-items:center}
.station-choice{display:flex;align-items:center;justify-content:center;gap:5px;background:#0d141e;border:1px solid var(--border);border-radius:10px;padding:10px;cursor:pointer}
.station-choice input{width:auto;margin:0}.recipient-filter{padding:16px 0;border-bottom:1px solid #213044}.recipient-filter:last-child{border-bottom:0}.recipient-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:10px}
@media(max-width:880px){.station-grid{grid-template-columns:repeat(4,1fr)}.station-grid .btn{grid-column:span 4}}
</style>
<div class="wrap">
  <div class="top"><div class="brand"><h1>Stationsfilter</h1><p>SBR Pager · vælg alarmer pr. WhatsApp-modtager</p></div><div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div></div>
  <div class="grid"><section class="card span12">
  {% for item in station_recipients %}
  <form class="recipient-filter" method="post" action="{{ url_for('update_recipient_stations', recipient_id=item.recipient.id) }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="recipient-head"><strong>{{ item.recipient.name }}</strong><span class="muted">{{ item.recipient.phone }}</span></div>
    <div class="station-grid">
      <label class="station-choice"><input type="checkbox" name="stations" value="*" {{ 'checked' if '*' in item.selected else '' }}>Alle</label>
      {% for code in stations %}<label class="station-choice"><input type="checkbox" name="stations" value="{{ code }}" {{ 'checked' if code in item.selected else '' }}>{{ 'Test' if code == 'TEST' else code }}</label>{% endfor %}
      <button class="btn primary small" type="submit">Gem filter</button>
    </div>
  </form>
  {% else %}<div class="empty">Ingen WhatsApp-modtagere endnu.</div>{% endfor %}
  </section></div>
</div>
""",
)


def station_rows():
    recipients = base.Recipient.query.order_by(base.Recipient.name).all()
    return [
        {"recipient": recipient, "selected": configured_stations(recipient.id)}
        for recipient in recipients
    ]


_previous_dashboard = app.view_functions["dashboard"]


def dashboard_with_station_filters():
    raw_response = _previous_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response

    fragment = render_template_string(
        STATION_FILTER_FRAGMENT + PREALERT_SETTING_FRAGMENT,
        station_recipients=station_rows(),
        stations=STATIONS,
        prealert_delay=format_seconds(prealert_delay_seconds()),
    )
    html = response.get_data(as_text=True)

    # stats_v2 renames the technical section, so support both the current and
    # older heading. This was the reason the first version could disappear.
    markers = [
        '<section class="card span12"><h2>Teknisk SMS-log</h2>',
        '<section class="card span12"><h2>Seneste SMS\'er</h2>',
    ]
    inserted = False
    for marker in markers:
        if marker in html:
            html = html.replace(marker, fragment + marker, 1)
            inserted = True
            break

    if not inserted:
        # Last-resort: place it before the footer/closing grid without depending
        # on a section heading.
        footer_marker = '<div class="footer">'
        if footer_marker in html:
            html = html.replace(footer_marker, fragment + footer_marker, 1)
            inserted = True

    # Always expose a dedicated page from the top actions as well, so the
    # setting stays reachable even if the dashboard layout changes later.
    logout_link = f'<a class="btn" href="{url_for("logout")}">Log ud</a>'
    filter_link = f'<a class="btn" href="{url_for("station_filters_page")}">Stationsfilter</a>'
    if filter_link not in html:
        html = html.replace(logout_link, filter_link + logout_link, 1)

    response.set_data(html)
    return response


app.view_functions["dashboard"] = dashboard_with_station_filters


@app.get("/stationsfilter")
@base.login_required
def station_filters_page():
    return render_template_string(
        STATION_FILTER_PAGE,
        title="Stationsfilter",
        station_recipients=station_rows(),
        stations=STATIONS,
    )


@app.get("/api/settings/prealert-delay")
def api_prealert_delay():
    base.require_ingest_token()
    value = prealert_delay_seconds()
    return jsonify(seconds=value, source="admin" if PagerRuntimeSetting.query.filter_by(key=PREALERT_DELAY_KEY).first() else "default")


@app.post("/indstillinger/prealarm-delay")
@base.login_required
def update_prealert_delay():
    base.check_csrf()
    raw = (request.form.get("seconds") or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        flash("Delay-tiden skal være et tal.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    if value < 0 or value > 120:
        flash("Delay-tiden skal være mellem 0 og 120 sekunder.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    value = round(value, 1)
    row = PagerRuntimeSetting.query.filter_by(key=PREALERT_DELAY_KEY).first()
    if row is None:
        row = PagerRuntimeSetting(key=PREALERT_DELAY_KEY, value=format_seconds(value))
        db.session.add(row)
    else:
        row.value = format_seconds(value)
        row.updated_at = base.utcnow()
    db.session.commit()

    flash(f"Pre-alarm delay er sat til {format_seconds(value)} sekunder.")
    return redirect(request.referrer or url_for("dashboard"))


def test_message_filtered():
    base.check_csrf()
    recipients = base.Recipient.query.filter_by(active=True).order_by(base.Recipient.name).all()
    recipients = [
        recipient
        for recipient in recipients
        if recipient_accepts(recipient.id, TEST_STATION)
    ]
    if not recipients:
        flash("Ingen aktive modtagere har valgt Test.", "error")
        return redirect(url_for("dashboard"))

    sent = 0
    failed = 0
    for recipient in recipients:
        delivery = base.WhatsAppDelivery(
            inbound_id=None,
            recipient_name=recipient.name,
            recipient_phone=recipient.phone,
            status="pending",
            attempted_at=base.utcnow(),
        )
        db.session.add(delivery)
        db.session.commit()
        try:
            delivery.message_id = base.send_whatsapp(
                recipient.phone,
                "✅ TEST · Testbesked fra SBR Pager",
            )
            delivery.status = "sent"
            delivery.error = None
            sent += 1
        except Exception as exc:  # noqa: BLE001
            delivery.status = "failed"
            delivery.error = str(exc)[:1000]
            failed += 1
        delivery.attempted_at = base.utcnow()
        db.session.commit()

    flash(
        f"Test afsluttet: {sent} sendt til Test-modtagere, {failed} fejlet.",
        "error" if failed else "message",
    )
    return redirect(url_for("dashboard"))


# Den oprindelige /test-route bevares, men routing følger nu Test-filteret.
app.view_functions["test_message"] = base.login_required(test_message_filtered)


@app.post("/recipients/<int:recipient_id>/stations")
@base.login_required
def update_recipient_stations(recipient_id: int):
    base.check_csrf()
    recipient = db.get_or_404(base.Recipient, recipient_id)
    selected = {value.upper() for value in request.form.getlist("stations")}
    selected &= set(STATIONS) | {ALL_STATIONS}

    # Tomt valg betyder fortsat Alle. "Alle" må gerne kombineres med Test,
    # men almindelige stationskoder er overflødige, når Alle er valgt.
    if not selected:
        selected = {ALL_STATIONS}
    elif ALL_STATIONS in selected:
        selected = {ALL_STATIONS} | (
            {TEST_STATION} if TEST_STATION in selected else set()
        )

    RecipientStationFilter.query.filter_by(recipient_id=recipient.id).delete()
    for station in sorted(selected):
        db.session.add(RecipientStationFilter(recipient_id=recipient.id, station=station))
    db.session.commit()

    flash(f"Stationsfilter for {recipient.name}: {selection_label(selected)}")
    next_url = request.form.get("next") or request.referrer
    if next_url and request.host_url.rstrip("/") in next_url:
        return redirect(next_url)
    return redirect(url_for("station_filters_page"))


with app.app_context():
    db.create_all()
