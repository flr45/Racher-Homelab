from __future__ import annotations

import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import render_template_string, request, url_for
from sqlalchemy.exc import IntegrityError

import app as base

app = base.app
db = base.db
log = logging.getLogger("sms-whatsapp-events")
COPENHAGEN = ZoneInfo("Europe/Copenhagen")
EVENT_LINK_MINUTES = max(15, int(os.getenv("SMS_WHATSAPP_EVENT_LINK_MINUTES", "120")))


class AlarmEvent(db.Model):
    __tablename__ = "alarm_event"

    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(128), unique=True, nullable=False, index=True)
    sender = db.Column(db.String(20), nullable=False, index=True)
    station = db.Column(db.String(20), index=True)
    alarm_type = db.Column(db.String(160), index=True)
    address = db.Column(db.String(255))
    status = db.Column(db.String(24), nullable=False, default="waiting", index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    first_alerted_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    last_update_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    followup_count = db.Column(db.Integer, nullable=False, default=0)


class AlarmEventMessage(db.Model):
    __tablename__ = "alarm_event_message"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("alarm_event.id"), nullable=False, index=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id"), unique=True, nullable=False, index=True)
    group_key = db.Column(db.String(128), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    raw_body = db.Column(db.Text, nullable=False)
    part_current = db.Column(db.Integer)
    part_total = db.Column(db.Integer)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: base.utcnow())


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def dk_time(value: datetime | None, fmt: str = "%d/%m %H:%M:%S") -> str:
    parsed = aware(value)
    return parsed.astimezone(COPENHAGEN).strftime(fmt) if parsed else "—"


app.jinja_env.globals["dk_time"] = dk_time


def positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def is_simple_test(raw_body: str) -> bool:
    compact = " ".join((raw_body or "").casefold().split())
    if "m+v" in compact or "·" in raw_body:
        return False
    return compact.startswith("test") and len(compact) < 80


def infer_kind(body: str, raw_body: str) -> str:
    haystack = f"{body}\n{raw_body}".casefold()
    if is_simple_test(raw_body):
        return "test"
    if "sending 2" in haystack:
        return "sending2_prealert" if "afventer resten" in haystack else "sending2_complete"
    if "alarm modtaget" in haystack and "afventer resten" in haystack:
        return "alarm_prealert"
    return "alarm_complete"


def extract_event_fields(raw_body: str) -> tuple[str | None, str | None, str | None]:
    text = (raw_body or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = " ".join(lines)

    station_match = re.search(r"\(([A-ZÆØÅ0-9]{1,6})\)", joined, re.IGNORECASE)
    station = station_match.group(1).upper() if station_match else None

    alarm_type = None
    segments = [part.strip() for part in re.split(r"\s*·\s*", joined) if part.strip()]
    if len(segments) >= 2:
        candidate = segments[1]
        if not re.search(r"\bsending\s*2\b", candidate, re.IGNORECASE):
            alarm_type = candidate[:160]

    address = None
    address_candidates = lines + segments
    street_pattern = re.compile(
        r"\b[\wÆØÅæøå.'-]+(?:\s+[\wÆØÅæøå.'-]+){0,4}\s+\d{1,4}[A-Za-z]?\b.*\b(?:vej|gade|all[eé]|stræde|vænget|boulevard|torv|plads|stien|parken)\b|"
        r"\b[\wÆØÅæøå.'-]+(?:vej|gade|all[eé]|stræde|vænget|boulevard|torv|plads|stien|parken)\s+\d{1,4}[A-Za-z]?\b",
        re.IGNORECASE,
    )
    for candidate in address_candidates:
        if re.search(r"\b\d{4}\b", candidate) and re.search(r"\d", candidate):
            address = candidate[:255]
            break
        if street_pattern.search(candidate):
            address = candidate[:255]
            break

    return station, alarm_type, address


def first_sent_delivery_at(inbound_id: int) -> datetime | None:
    delivery = (
        base.WhatsAppDelivery.query.filter_by(inbound_id=inbound_id, status="sent")
        .order_by(base.WhatsAppDelivery.attempted_at.asc(), base.WhatsAppDelivery.id.asc())
        .first()
    )
    return delivery.attempted_at if delivery is not None else None


def mark_first_delivery(inbound_id: int, delivered_at: datetime | None = None) -> None:
    """Record the first actual successful WhatsApp delivery for an event."""
    message = AlarmEventMessage.query.filter_by(inbound_id=inbound_id).first()
    if message is None:
        # Immediate delivery happens before the event wrapper records the
        # AlarmEventMessage. record_alarm_event() picks it up afterwards.
        return
    event = db.session.get(AlarmEvent, message.event_id)
    if event is None:
        return
    when = delivered_at or first_sent_delivery_at(inbound_id) or base.utcnow()
    current = aware(event.first_alerted_at)
    candidate = aware(when)
    if candidate is not None and (current is None or candidate < current):
        event.first_alerted_at = candidate
        db.session.commit()


def create_event(event_key: str, inbound: base.InboundMessage, raw_body: str) -> AlarmEvent:
    station, alarm_type, address = extract_event_fields(raw_body)
    event = AlarmEvent(
        event_key=event_key[:128],
        sender=inbound.sender,
        station=station,
        alarm_type=alarm_type,
        address=address,
        status="waiting",
        started_at=inbound.received_at,
        first_alerted_at=first_sent_delivery_at(inbound.id),
        last_update_at=inbound.created_at,
        followup_count=0,
    )
    db.session.add(event)
    try:
        db.session.commit()
        return event
    except IntegrityError:
        db.session.rollback()
        existing = AlarmEvent.query.filter_by(event_key=event_key[:128]).first()
        if existing is None:
            raise
        return existing


def recent_parent_event(inbound: base.InboundMessage) -> AlarmEvent | None:
    received = aware(inbound.received_at) or base.utcnow()
    lower_bound = received - timedelta(minutes=EVENT_LINK_MINUTES)
    return (
        AlarmEvent.query.filter(
            AlarmEvent.sender == inbound.sender,
            AlarmEvent.started_at >= lower_bound,
            AlarmEvent.started_at <= received + timedelta(minutes=5),
        )
        .order_by(AlarmEvent.started_at.desc())
        .first()
    )


def record_alarm_event(inbound: base.InboundMessage, payload: dict) -> None:
    if AlarmEventMessage.query.filter_by(inbound_id=inbound.id).first() is not None:
        return

    raw_body = str(payload.get("rawBody") or inbound.body or "").strip()
    kind = str(payload.get("messageKind") or infer_kind(inbound.body, raw_body)).strip()[:40]
    if kind == "test":
        return

    event_key = str(payload.get("eventKey") or payload.get("groupKey") or inbound.source_id).strip()[:128]
    group_key = str(payload.get("groupKey") or event_key).strip()[:128]
    parent_event_key = str(payload.get("parentEventKey") or "").strip()[:128]
    is_followup = kind.startswith(("sending2", "followup"))

    if is_followup:
        event = (
            AlarmEvent.query.filter_by(event_key=parent_event_key).first()
            if parent_event_key
            else None
        )
        if event is None:
            event = recent_parent_event(inbound)
        if event is None:
            event = create_event(f"orphan:{event_key}"[:128], inbound, raw_body)
    else:
        event = AlarmEvent.query.filter_by(event_key=event_key).first()
        if event is None:
            event = create_event(event_key, inbound, raw_body)

    existing_group = (
        AlarmEventMessage.query.filter_by(event_id=event.id, group_key=group_key).first()
        is not None
    )

    message = AlarmEventMessage(
        event_id=event.id,
        inbound_id=inbound.id,
        group_key=group_key,
        kind=kind,
        raw_body=raw_body,
        part_current=positive_int(payload.get("partCurrent")),
        part_total=positive_int(payload.get("partTotal")),
        received_at=inbound.received_at,
        created_at=inbound.created_at,
    )
    db.session.add(message)

    station, alarm_type, address = extract_event_fields(raw_body)
    if station:
        event.station = station
    if alarm_type:
        event.alarm_type = alarm_type
    if address:
        event.address = address

    event.last_update_at = inbound.created_at
    delivered_at = first_sent_delivery_at(inbound.id)
    if delivered_at is not None:
        current_delivery = aware(event.first_alerted_at)
        candidate_delivery = aware(delivered_at)
        if candidate_delivery is not None and (
            current_delivery is None or candidate_delivery < current_delivery
        ):
            event.first_alerted_at = candidate_delivery
    if (aware(inbound.received_at) or base.utcnow()) < (aware(event.started_at) or base.utcnow()):
        event.started_at = inbound.received_at

    if is_followup:
        if not existing_group:
            event.followup_count = (event.followup_count or 0) + 1
    elif kind == "alarm_prealert":
        if event.status != "complete":
            event.status = "waiting"
    else:
        event.status = "complete"
        event.completed_at = inbound.created_at

    db.session.commit()


def stats_snapshot() -> dict:
    now_utc = base.utcnow()
    now_dk = now_utc.astimezone(COPENHAGEN)
    today_utc = now_dk.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    start_7d = now_utc - timedelta(days=7)
    start_30d = now_utc - timedelta(days=30)

    today = AlarmEvent.query.filter(AlarmEvent.started_at >= today_utc).count()
    days7 = AlarmEvent.query.filter(AlarmEvent.started_at >= start_7d).count()
    days30 = AlarmEvent.query.filter(AlarmEvent.started_at >= start_30d).count()
    waiting = AlarmEvent.query.filter_by(status="waiting").count()

    recent = AlarmEvent.query.filter(AlarmEvent.started_at >= start_30d).all()
    first_delays = []
    complete_delays = []
    followups = 0
    type_counter = Counter()
    hour_counter = Counter()

    for event in recent:
        started = aware(event.started_at)
        first = aware(event.first_alerted_at)
        completed = aware(event.completed_at)
        if started and first:
            first_delays.append(max(0.0, (first - started).total_seconds()))
        if started and completed:
            complete_delays.append(max(0.0, (completed - started).total_seconds()))
        followups += event.followup_count or 0
        if event.alarm_type:
            type_counter[event.alarm_type] += 1
        if started:
            hour_counter[started.astimezone(COPENHAGEN).hour] += 1

    return {
        "today": today,
        "days7": days7,
        "days30": days30,
        "waiting": waiting,
        "followups": followups,
        "avg_first": round(sum(first_delays) / len(first_delays), 1) if first_delays else None,
        "avg_complete": round(sum(complete_delays) / len(complete_delays), 1) if complete_delays else None,
        "top_types": type_counter.most_common(8),
        "hours": sorted(hour_counter.items()),
    }


DASHBOARD_STATS_FRAGMENT = r"""
<style>
.span3{grid-column:span 3}.statlink{text-decoration:none}.statlink .card{height:100%}
@media(max-width:880px){.span3{grid-column:span 6}}@media(max-width:520px){.span3{grid-column:span 12}}
</style>
<a class="span3 statlink" href="{{ url_for('alarm_statistics') }}"><section class="card"><h2>Alarmer i dag</h2><div class="metric">{{ stats.today }}</div><p class="muted">Registrerede hændelser siden midnat.</p></section></a>
<a class="span3 statlink" href="{{ url_for('alarm_statistics') }}"><section class="card"><h2>Seneste 7 dage</h2><div class="metric">{{ stats.days7 }}</div><p class="muted">Samlede alarmhændelser.</p></section></a>
<a class="span3 statlink" href="{{ url_for('alarm_statistics') }}"><section class="card"><h2>Første varsling</h2><div class="metric">{{ (stats.avg_first|string + ' s') if stats.avg_first is not none else '—' }}</div><p class="muted">Gns. fra SMS-tid til første WhatsApp.</p></section></a>
<a class="span3 statlink" href="{{ url_for('alarm_statistics') }}"><section class="card"><h2>Komplet melding</h2><div class="metric">{{ (stats.avg_complete|string + ' s') if stats.avg_complete is not none else '—' }}</div><p class="muted">Gns. ventetid på alle SMS-dele.</p></section></a>
"""


RECENT_EVENTS_FRAGMENT = r"""
<section class="card span12"><div class="top" style="margin-bottom:12px"><div><h2 style="margin:0">Seneste alarmhændelser</h2><p class="muted" style="margin:5px 0 0">Første alarm og efterfølgende Sending 2 samlet som én hændelse.</p></div><a class="btn" href="{{ url_for('alarm_statistics') }}">Alarmstatistik</a></div>
<div class="tablewrap"><table><thead><tr><th>Tid</th><th>Station</th><th>Alarmtype</th><th>Adresse</th><th>Status</th><th>Sendinger</th><th></th></tr></thead><tbody>
{% for event in events %}<tr><td>{{ dk_time(event.started_at) }}</td><td>{{ event.station or '—' }}</td><td>{{ event.alarm_type or '—' }}</td><td class="bodycell">{{ event.address or '—' }}</td><td><span class="tag">{{ 'Komplet' if event.status == 'complete' else 'Afventer' }}</span></td><td>{{ event.followup_count or 0 }}</td><td><a class="btn small" href="{{ url_for('alarm_event_detail', event_id=event.id) }}">Vis</a></td></tr>
{% else %}<tr><td colspan="7" class="empty">Ingen hændelser registreret endnu.</td></tr>{% endfor %}
</tbody></table></div></section>
"""


STATISTICS_HTML = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<style>.span3{grid-column:span 3}.bar{display:flex;gap:10px;align-items:center;margin:8px 0}.barlabel{width:58px;color:var(--muted)}.bartrack{height:10px;flex:1;border-radius:99px;background:#0d141e;overflow:hidden}.barfill{height:100%;background:var(--green);border-radius:99px}.barvalue{width:32px;text-align:right}@media(max-width:880px){.span3{grid-column:span 6}}@media(max-width:520px){.span3{grid-column:span 12}}</style>
<div class="wrap">
<div class="top"><div class="brand"><h1>Alarmstatistik</h1><p>SBR Pager · hændelser og leveringstider</p></div><div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div></div>
<div class="grid">
<section class="card span3"><h2>I dag</h2><div class="metric">{{ stats.today }}</div><p class="muted">Alarmhændelser</p></section>
<section class="card span3"><h2>7 dage</h2><div class="metric">{{ stats.days7 }}</div><p class="muted">Alarmhændelser</p></section>
<section class="card span3"><h2>30 dage</h2><div class="metric">{{ stats.days30 }}</div><p class="muted">{{ stats.followups }} Sending 2</p></section>
<section class="card span3"><h2>Afventer</h2><div class="metric">{{ stats.waiting }}</div><p class="muted">Multipart-meldinger uden sidste del</p></section>
<section class="card span6"><h2>Leveringstid · seneste 30 dage</h2><table><tbody><tr><td>Første WhatsApp-varsling</td><td><strong>{{ (stats.avg_first|string + ' sek.') if stats.avg_first is not none else '—' }}</strong></td></tr><tr><td>Komplet alarmmelding</td><td><strong>{{ (stats.avg_complete|string + ' sek.') if stats.avg_complete is not none else '—' }}</strong></td></tr></tbody></table></section>
<section class="card span6"><h2>Mest almindelige alarmtyper · 30 dage</h2><table><tbody>{% for name,count in stats.top_types %}<tr><td>{{ name }}</td><td>{{ count }}</td></tr>{% else %}<tr><td class="empty">Ikke nok data endnu.</td></tr>{% endfor %}</tbody></table></section>
<section class="card span12"><h2>Tidspunkt på døgnet · 30 dage</h2>{% set maxhour = (stats.hours | map(attribute=1) | max) if stats.hours else 1 %}{% for hour,count in stats.hours %}<div class="bar"><div class="barlabel">{{ '%02d'|format(hour) }}–{{ '%02d'|format((hour+1)%24) }}</div><div class="bartrack"><div class="barfill" style="width:{{ (count / maxhour * 100)|round }}%"></div></div><div class="barvalue">{{ count }}</div></div>{% else %}<div class="empty">Ikke nok data endnu.</div>{% endfor %}</section>
<section class="card span12"><h2>Seneste hændelser</h2><div class="tablewrap"><table><thead><tr><th>Tid</th><th>Station</th><th>Alarmtype</th><th>Adresse</th><th>Status</th><th>Sending 2</th><th></th></tr></thead><tbody>{% for event in events %}<tr><td>{{ dk_time(event.started_at) }}</td><td>{{ event.station or '—' }}</td><td>{{ event.alarm_type or '—' }}</td><td class="bodycell">{{ event.address or '—' }}</td><td><span class="tag">{{ 'Komplet' if event.status == 'complete' else 'Afventer' }}</span></td><td>{{ event.followup_count or 0 }}</td><td><a class="btn small" href="{{ url_for('alarm_event_detail', event_id=event.id) }}">Vis</a></td></tr>{% else %}<tr><td colspan="7" class="empty">Ingen hændelser registreret endnu.</td></tr>{% endfor %}</tbody></table></div></section>
</div><div class="footer">Statistikken starter fra aktiveringen af hændelsesmodulet. Tider vises i Europe/Copenhagen.</div>
</div>
""",
)


EVENT_DETAIL_HTML = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<div class="wrap">
<div class="top"><div class="brand"><h1>Alarmhændelse #{{ event.id }}</h1><p>{{ dk_time(event.started_at) }} · {{ event.sender }}</p></div><div class="actions"><a class="btn" href="{{ url_for('alarm_statistics') }}">← Alarmstatistik</a><a class="btn" href="{{ url_for('dashboard') }}">Administration</a></div></div>
<div class="grid">
<section class="card span4"><h2>Status</h2><div class="metric">{{ 'Komplet' if event.status == 'complete' else 'Afventer' }}</div><p class="muted">Første varsling: {{ dk_time(event.first_alerted_at) }}</p></section>
<section class="card span4"><h2>Alarm</h2><div class="metric" style="font-size:18px">{{ event.alarm_type or 'Ukendt type' }}</div><p class="muted">Station {{ event.station or '—' }}</p></section>
<section class="card span4"><h2>Adresse</h2><div style="font-size:17px;font-weight:700">{{ event.address or 'Ikke fundet automatisk' }}</div><p class="muted">Sending 2: {{ event.followup_count or 0 }}</p></section>
<section class="card span12"><h2>Tidslinje</h2><div class="tablewrap"><table><thead><tr><th>Modtaget</th><th>Type</th><th>Dele</th><th>Original tekst</th><th>WhatsApp</th></tr></thead><tbody>{% for row in timeline %}<tr><td>{{ dk_time(row.message.received_at) }}</td><td><span class="tag">{{ row.label }}</span></td><td>{{ (row.message.part_current|string + '/' + row.message.part_total|string) if row.message.part_total else '1/1' }}</td><td class="bodycell">{{ row.message.raw_body }}</td><td>{% for d in row.deliveries %}<div>{{ d.recipient_name }} · {{ d.status }}</div>{% else %}<span class="muted">Ingen leveringer</span>{% endfor %}</td></tr>{% endfor %}</tbody></table></div></section>
</div></div>
""",
)


def kind_label(kind: str) -> str:
    return {
        "alarm_prealert": "Første varsling",
        "alarm_complete": "Komplet alarm",
        "sending2_prealert": "Sending 2 · varsling",
        "sending2_complete": "Sending 2 · komplet",
        "followup_prealert": "Sending 2 · varsling",
        "followup_complete": "Sending 2 · komplet",
    }.get(kind, kind)


_original_incoming = app.view_functions["incoming"]
_original_dashboard = app.view_functions["dashboard"]


def incoming_with_events():
    payload = request.get_json(silent=True) or {}
    response = _original_incoming()
    try:
        status = response[1] if isinstance(response, tuple) else getattr(response, "status_code", 200)
        if 200 <= int(status) < 300:
            sender = base.normalize_phone(payload.get("sender", ""))
            body = str(payload.get("body", "")).strip()
            received_at = base.parse_received_at(payload.get("receivedAt"))
            source_id = base.make_source_id(sender, body, received_at, payload.get("sourceMessageId"))
            inbound = base.InboundMessage.query.filter_by(source_id=source_id).first()
            if inbound is not None and inbound.accepted:
                record_alarm_event(inbound, payload)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("Kunne ikke registrere alarmhændelse; WhatsApp-leveringen er allerede behandlet")
    return response


def dashboard_with_stats():
    raw_response = _original_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response

    html = response.get_data(as_text=True)
    stats = stats_snapshot()
    events = AlarmEvent.query.order_by(AlarmEvent.started_at.desc()).limit(10).all()
    stats_fragment = render_template_string(DASHBOARD_STATS_FRAGMENT, stats=stats)
    events_fragment = render_template_string(RECENT_EVENTS_FRAGMENT, events=events)
    html = html.replace('<div class="grid">', '<div class="grid">' + stats_fragment, 1)
    html = html.replace('<section class="card span12"><h2>Seneste SMS\'er</h2>', events_fragment + '<section class="card span12"><h2>Seneste SMS\'er</h2>', 1)

    logout_link = f'<a class="btn" href="{url_for("logout")}">Log ud</a>'
    stats_link = f'<a class="btn" href="{url_for("alarm_statistics")}">Alarmstatistik</a>'
    html = html.replace(logout_link, stats_link + logout_link, 1)
    response.set_data(html)
    return response


app.view_functions["incoming"] = incoming_with_events
app.view_functions["dashboard"] = dashboard_with_stats


@app.get("/alarmer")
@base.login_required
def alarm_statistics():
    events = AlarmEvent.query.order_by(AlarmEvent.started_at.desc()).limit(100).all()
    return render_template_string(STATISTICS_HTML, title="Alarmstatistik", stats=stats_snapshot(), events=events)


@app.get("/alarmer/<int:event_id>")
@base.login_required
def alarm_event_detail(event_id: int):
    event = db.get_or_404(AlarmEvent, event_id)
    messages = AlarmEventMessage.query.filter_by(event_id=event.id).order_by(AlarmEventMessage.received_at, AlarmEventMessage.id).all()
    timeline = []
    for message in messages:
        deliveries = base.WhatsAppDelivery.query.filter_by(inbound_id=message.inbound_id).order_by(base.WhatsAppDelivery.attempted_at).all()
        timeline.append({"message": message, "deliveries": deliveries, "label": kind_label(message.kind)})
    return render_template_string(EVENT_DETAIL_HTML, title=f"Alarm #{event.id}", event=event, timeline=timeline)


with app.app_context():
    db.create_all()
