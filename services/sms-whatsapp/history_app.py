from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta, timezone

from flask import flash, redirect, render_template_string, request, url_for

import events_app as events
import ui_app as previous

app = previous.app
base = events.base
db = events.db


class AlarmEventLocation(db.Model):
    __tablename__ = "alarm_event_location"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("alarm_event.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=base.utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=base.utcnow, onupdate=base.utcnow)


def _station_filter(value: str | None) -> str | None:
    cleaned = (value or "").strip().upper()
    return cleaned or None


def _event_query(station: str | None = None):
    query = events.AlarmEvent.query
    if station:
        query = query.filter(events.AlarmEvent.station == station)
    return query


def filtered_stats(station: str | None = None) -> dict:
    now_utc = base.utcnow()
    now_dk = now_utc.astimezone(events.COPENHAGEN)
    today_utc = now_dk.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    start_7d = now_utc - timedelta(days=7)
    start_30d = now_utc - timedelta(days=30)

    query = _event_query(station)
    today = query.filter(events.AlarmEvent.started_at >= today_utc).count()
    days7 = query.filter(events.AlarmEvent.started_at >= start_7d).count()
    days30 = query.filter(events.AlarmEvent.started_at >= start_30d).count()
    waiting = query.filter(events.AlarmEvent.status == "waiting").count()
    recent = query.filter(events.AlarmEvent.started_at >= start_30d).all()

    first_delays: list[float] = []
    complete_delays: list[float] = []
    followups = 0
    type_counter: Counter[str] = Counter()
    hour_counter: Counter[int] = Counter()

    for event in recent:
        started = events.aware(event.started_at)
        first = events.aware(event.first_alerted_at)
        completed = events.aware(event.completed_at)
        if started and first:
            first_delays.append(max(0.0, (first - started).total_seconds()))
        if started and completed:
            complete_delays.append(max(0.0, (completed - started).total_seconds()))
        followups += event.followup_count or 0
        if event.alarm_type:
            type_counter[event.alarm_type] += 1
        if started:
            hour_counter[started.astimezone(events.COPENHAGEN).hour] += 1

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


def station_statistics() -> list[dict]:
    cutoff = base.utcnow() - timedelta(days=30)
    grouped: dict[str, list[events.AlarmEvent]] = defaultdict(list)
    for event in events.AlarmEvent.query.filter(events.AlarmEvent.started_at >= cutoff).all():
        grouped[event.station or "Ukendt"].append(event)

    result = []
    for station, rows in grouped.items():
        first_delays = []
        complete_delays = []
        followups = 0
        for event in rows:
            started = events.aware(event.started_at)
            first = events.aware(event.first_alerted_at)
            completed = events.aware(event.completed_at)
            if started and first:
                first_delays.append(max(0.0, (first - started).total_seconds()))
            if started and completed:
                complete_delays.append(max(0.0, (completed - started).total_seconds()))
            followups += event.followup_count or 0
        result.append(
            {
                "station": station,
                "count": len(rows),
                "followups": followups,
                "avg_first": round(sum(first_delays) / len(first_delays), 1) if first_delays else None,
                "avg_complete": round(sum(complete_delays) / len(complete_delays), 1) if complete_delays else None,
            }
        )
    return sorted(result, key=lambda row: (-row["count"], row["station"]))


def station_options() -> list[str]:
    rows = (
        db.session.query(events.AlarmEvent.station)
        .filter(events.AlarmEvent.station.is_not(None))
        .distinct()
        .order_by(events.AlarmEvent.station)
        .all()
    )
    return [row[0] for row in rows if row[0]]


def event_location(event_id: int) -> AlarmEventLocation | None:
    return AlarmEventLocation.query.filter_by(event_id=event_id).first()


STATISTICS_HTML = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<style>
.span3{grid-column:span 3}.span6{grid-column:span 6}.bar{display:flex;gap:10px;align-items:center;margin:8px 0}.barlabel{width:58px;color:var(--muted)}.bartrack{height:10px;flex:1;border-radius:99px;background:#0d141e;overflow:hidden}.barfill{height:100%;background:var(--green);border-radius:99px}.barvalue{width:32px;text-align:right}.inline-actions{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.inline-actions form{margin:0}.filterbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.filterbar label{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}.filterbar select{min-width:170px;background:#09111a;border:1px solid var(--border);color:var(--text);border-radius:10px;padding:9px 10px}
@media(max-width:880px){.span3,.span6{grid-column:span 6}}@media(max-width:520px){.span3,.span6{grid-column:span 12}}
</style>
<div class="wrap">
<div class="top"><div class="brand"><h1>Alarmstatistik</h1><p>SBR Pager · hændelser, stationer og leveringstider{% if selected_station %} · Station {{ selected_station }}{% endif %}</p></div><div class="actions"><a class="btn" href="{{ url_for('alarm_map') }}">Alarmkort</a><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div></div>

<section class="card span12" style="margin-bottom:16px">
<form class="filterbar" method="get" action="{{ url_for('alarm_statistics') }}">
  <div><label>Vis station</label><select name="station"><option value="">Alle stationer</option>{% for code in stations %}<option value="{{ code }}" {{ 'selected' if code == selected_station else '' }}>Station {{ code }}</option>{% endfor %}</select></div>
  <button class="btn primary" type="submit">Filtrér</button>{% if selected_station %}<a class="btn" href="{{ url_for('alarm_statistics') }}">Nulstil</a>{% endif %}
</form>
</section>

<div class="grid">
<section class="card span3"><h2>I dag</h2><div class="metric">{{ stats.today }}</div><p class="muted">Alarmhændelser</p></section>
<section class="card span3"><h2>7 dage</h2><div class="metric">{{ stats.days7 }}</div><p class="muted">Alarmhændelser</p></section>
<section class="card span3"><h2>30 dage</h2><div class="metric">{{ stats.days30 }}</div><p class="muted">{{ stats.followups }} Sending 2/opfølgninger</p></section>
<section class="card span3"><h2>Afventer</h2><div class="metric">{{ stats.waiting }}</div><p class="muted">Multipart-meldinger uden sidste del</p></section>
<section class="card span6"><h2>Leveringstid · seneste 30 dage</h2><table><tbody><tr><td>Første registrerede varsling</td><td><strong>{{ (stats.avg_first|string + ' sek.') if stats.avg_first is not none else '—' }}</strong></td></tr><tr><td>Komplet alarmmelding</td><td><strong>{{ (stats.avg_complete|string + ' sek.') if stats.avg_complete is not none else '—' }}</strong></td></tr></tbody></table></section>
<section class="card span6"><h2>Mest almindelige alarmtyper · 30 dage</h2><table><tbody>{% for name,count in stats.top_types %}<tr><td>{{ name }}</td><td>{{ count }}</td></tr>{% else %}<tr><td class="empty">Ikke nok data endnu.</td></tr>{% endfor %}</tbody></table></section>

<section class="card span12"><div class="top" style="margin-bottom:10px"><div><h2 style="margin:0">Statistik pr. station · 30 dage</h2><p class="muted" style="margin:5px 0 0">Klik på en station for at filtrere hele statistiksiden.</p></div></div><div class="tablewrap"><table><thead><tr><th>Station</th><th>Alarmer</th><th>Sending 2</th><th>Første varsling</th><th>Komplet melding</th><th></th></tr></thead><tbody>{% for row in station_stats %}<tr><td><strong>{{ row.station }}</strong></td><td>{{ row.count }}</td><td>{{ row.followups }}</td><td>{{ (row.avg_first|string + ' s') if row.avg_first is not none else '—' }}</td><td>{{ (row.avg_complete|string + ' s') if row.avg_complete is not none else '—' }}</td><td>{% if row.station != 'Ukendt' %}<a class="btn small" href="{{ url_for('alarm_statistics', station=row.station) }}">Vis</a>{% endif %}</td></tr>{% else %}<tr><td colspan="6" class="empty">Ikke nok data endnu.</td></tr>{% endfor %}</tbody></table></div></section>

<section class="card span12"><h2>Tidspunkt på døgnet · 30 dage</h2>{% set maxhour = (stats.hours | map(attribute=1) | max) if stats.hours else 1 %}{% for hour,count in stats.hours %}<div class="bar"><div class="barlabel">{{ '%02d'|format(hour) }}–{{ '%02d'|format((hour+1)%24) }}</div><div class="bartrack"><div class="barfill" style="width:{{ (count / maxhour * 100)|round }}%"></div></div><div class="barvalue">{{ count }}</div></div>{% else %}<div class="empty">Ikke nok data endnu.</div>{% endfor %}</section>

<section class="card span12"><div class="top" style="margin-bottom:10px"><div><h2 style="margin:0">Alarmhistorik</h2><p class="muted" style="margin:5px 0 0">Slet testhændelser herfra, så de straks forsvinder fra statistikken.</p></div><a class="btn" href="{{ url_for('alarm_map') }}">Vis på kort</a></div><div class="tablewrap"><table><thead><tr><th>Tid</th><th>Station</th><th>Alarmtype</th><th>Adresse</th><th>Status</th><th>Sending 2</th><th></th></tr></thead><tbody>{% for event in alarm_events %}<tr><td>{{ dk_time(event.started_at) }}</td><td>{{ event.station or '—' }}</td><td>{{ event.alarm_type or '—' }}</td><td class="bodycell">{{ event.address or '—' }}</td><td><span class="tag">{{ 'Komplet' if event.status == 'complete' else 'Afventer' }}</span></td><td>{{ event.followup_count or 0 }}</td><td><div class="inline-actions"><a class="btn small" href="{{ url_for('alarm_event_detail', event_id=event.id) }}">Vis</a><form method="post" action="{{ url_for('delete_alarm_event', event_id=event.id) }}" onsubmit="return confirm('Slet denne alarmhændelse og dens SBR Pager-beskeder?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button class="btn danger small" type="submit">Slet</button></form></div></td></tr>{% else %}<tr><td colspan="7" class="empty">Ingen hændelser registreret endnu.</td></tr>{% endfor %}</tbody></table></div></section>
</div><div class="footer">Slettede hændelser indgår ikke længere i statistikken. Tider vises i Europe/Copenhagen.</div>
</div>
""",
)


EVENT_DETAIL_HTML = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>#eventMap{height:360px;border-radius:14px;border:1px solid var(--border);overflow:hidden}.map-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.coord-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.coord-grid input{width:100%;background:#09111a;border:1px solid var(--border);color:var(--text);border-radius:10px;padding:9px 10px}</style>
<div class="wrap">
<div class="top"><div class="brand"><h1>Alarmhændelse #{{ event.id }}</h1><p>{{ dk_time(event.started_at) }} · {{ event.sender }}</p></div><div class="actions"><a class="btn" href="{{ url_for('alarm_map') }}">Alarmkort</a><a class="btn" href="{{ url_for('alarm_statistics') }}">← Alarmstatistik</a><a class="btn" href="{{ url_for('dashboard') }}">Administration</a></div></div>
<div class="grid">
<section class="card span4"><h2>Status</h2><div class="metric">{{ 'Komplet' if event.status == 'complete' else 'Afventer' }}</div><p class="muted">Første varsling: {{ dk_time(event.first_alerted_at) }}</p></section>
<section class="card span4"><h2>Alarm</h2><div class="metric" style="font-size:18px">{{ event.alarm_type or 'Ukendt type' }}</div><p class="muted">Station {{ event.station or '—' }}</p></section>
<section class="card span4"><h2>Adresse</h2><div style="font-size:17px;font-weight:700">{{ event.address or 'Ikke fundet automatisk' }}</div><p class="muted">Sending 2: {{ event.followup_count or 0 }}</p></section>

<section class="card span12"><div class="top" style="margin-bottom:10px"><div><h2 style="margin:0">Kortplacering</h2><p class="muted" style="margin:5px 0 0">Klik på kortet for at placere turen. Positionen gemmes lokalt i SBR Pager.</p></div></div><div id="eventMap"></div><form method="post" action="{{ url_for('save_alarm_location', event_id=event.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div class="coord-grid"><input id="lat" name="latitude" inputmode="decimal" placeholder="Breddegrad" value="{{ location.latitude if location else '' }}" required><input id="lon" name="longitude" inputmode="decimal" placeholder="Længdegrad" value="{{ location.longitude if location else '' }}" required></div><div class="map-actions"><button class="btn primary" type="submit">Gem på alarmkort</button>{% if location %}</form><form method="post" action="{{ url_for('remove_alarm_location', event_id=event.id) }}" onsubmit="return confirm('Fjern kortplaceringen for denne tur?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn" type="submit">Fjern fra kort</button></form>{% else %}</form>{% endif %}</div></section>

<section class="card span12"><h2>Tidslinje</h2><div class="tablewrap"><table><thead><tr><th>Modtaget</th><th>Type</th><th>Dele</th><th>Original tekst</th><th>WhatsApp</th></tr></thead><tbody>{% for row in timeline %}<tr><td>{{ dk_time(row.message.received_at) }}</td><td><span class="tag">{{ row.label }}</span></td><td>{{ (row.message.part_current|string + '/' + row.message.part_total|string) if row.message.part_total else '1/1' }}</td><td class="bodycell">{{ row.message.raw_body }}</td><td>{% for d in row.deliveries %}<div>{{ d.recipient_name }} · {{ d.status }}</div>{% else %}<span class="muted">Ingen leveringer</span>{% endfor %}</td></tr>{% endfor %}</tbody></table></div></section>
<section class="card span12" style="border-color:#60313a;background:#24151a"><div class="top"><div><h2 style="margin:0">Slet hændelse</h2><p class="muted" style="margin:5px 0 0">Fjerner hændelsen, dens SBR Pager-beskeder, leveringslogs og kortplacering. Den forsvinder derefter fra statistikken.</p></div><form method="post" action="{{ url_for('delete_alarm_event', event_id=event.id) }}" onsubmit="return confirm('Er du sikker? Handlingen kan ikke fortrydes.')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn danger" type="submit">Slet hændelse</button></form></div></section>
</div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
  const savedLat = {{ location.latitude|tojson if location else 'null' }};
  const savedLon = {{ location.longitude|tojson if location else 'null' }};
  const start = (savedLat !== null && savedLon !== null) ? [savedLat, savedLon] : [55.402, 11.355];
  const map = L.map('eventMap').setView(start, (savedLat !== null ? 15 : 10));
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
  let marker = null;
  function setMarker(lat, lon){ if(marker){ marker.setLatLng([lat,lon]); } else { marker=L.marker([lat,lon]).addTo(map); } document.getElementById('lat').value=Number(lat).toFixed(6); document.getElementById('lon').value=Number(lon).toFixed(6); }
  if(savedLat !== null && savedLon !== null){ setMarker(savedLat, savedLon); }
  map.on('click', function(e){ setMarker(e.latlng.lat, e.latlng.lng); });
})();
</script>
""",
)


MAP_HTML = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>#alarmMap{height:70vh;min-height:520px;border-radius:16px;border:1px solid var(--border);overflow:hidden}.mapnote{margin-top:10px;color:var(--muted);font-size:13px}</style>
<div class="wrap">
<div class="top"><div class="brand"><h1>Alarmkort</h1><p>SBR Pager · gemte ture</p></div><div class="actions"><a class="btn" href="{{ url_for('alarm_statistics') }}">Alarmstatistik</a><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a></div></div>
<section class="card span12"><div class="top" style="margin-bottom:10px"><div><h2 style="margin:0">{{ points|length }} gemte ture</h2><p class="muted" style="margin:5px 0 0">Klik på en markør for at læse om turen og åbne hele hændelsen.</p></div></div><div id="alarmMap"></div><div class="mapnote">Kort: © OpenStreetMap contributors. Kun ture med en gemt kortplacering vises.</div></section>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
  const points = {{ points|tojson }};
  const map = L.map('alarmMap').setView([55.402, 11.355], 9);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
  const bounds=[];
  points.forEach(function(item){
    const marker=L.marker([item.lat,item.lon]).addTo(map); bounds.push([item.lat,item.lon]);
    const box=document.createElement('div');
    const title=document.createElement('strong'); title.textContent=(item.station ? 'Station '+item.station+' · ' : '')+(item.alarm_type || 'Alarm'); box.appendChild(title);
    const address=document.createElement('div'); address.textContent=item.address || 'Adresse ikke registreret'; box.appendChild(address);
    const time=document.createElement('div'); time.textContent=item.time; box.appendChild(time);
    const link=document.createElement('a'); link.href=item.url; link.textContent='Åbn tur'; link.style.display='inline-block'; link.style.marginTop='6px'; box.appendChild(link);
    marker.bindPopup(box);
  });
  if(bounds.length===1){ map.setView(bounds[0],15); } else if(bounds.length>1){ map.fitBounds(bounds,{padding:[25,25]}); }
})();
</script>
""",
)


def _safe_next(default: str) -> str:
    target = (request.form.get("next") or "").strip()
    return target if target.startswith("/") and not target.startswith("//") else default


def _timeline_for(event: events.AlarmEvent) -> list[dict]:
    messages = (
        events.AlarmEventMessage.query.filter_by(event_id=event.id)
        .order_by(events.AlarmEventMessage.received_at, events.AlarmEventMessage.id)
        .all()
    )
    timeline = []
    for message in messages:
        deliveries = (
            base.WhatsAppDelivery.query.filter_by(inbound_id=message.inbound_id)
            .order_by(base.WhatsAppDelivery.attempted_at)
            .all()
        )
        timeline.append({"message": message, "deliveries": deliveries, "label": events.kind_label(message.kind)})
    return timeline


def alarm_statistics_view():
    station = _station_filter(request.args.get("station"))
    alarm_events = _event_query(station).order_by(events.AlarmEvent.started_at.desc()).limit(200).all()
    return render_template_string(
        STATISTICS_HTML,
        title="Alarmstatistik",
        stats=filtered_stats(station),
        station_stats=station_statistics(),
        alarm_events=alarm_events,
        stations=station_options(),
        selected_station=station,
    )


def alarm_event_detail_view(event_id: int):
    event = db.get_or_404(events.AlarmEvent, event_id)
    return render_template_string(
        EVENT_DETAIL_HTML,
        title=f"Alarm #{event.id}",
        event=event,
        timeline=_timeline_for(event),
        location=event_location(event.id),
    )


app.view_functions["alarm_statistics"] = base.login_required(alarm_statistics_view)
app.view_functions["alarm_event_detail"] = base.login_required(alarm_event_detail_view)


@app.post("/alarmer/<int:event_id>/slet")
@base.login_required
def delete_alarm_event(event_id: int):
    base.check_csrf()
    event = db.get_or_404(events.AlarmEvent, event_id)
    messages = events.AlarmEventMessage.query.filter_by(event_id=event.id).all()
    inbound_ids = [row.inbound_id for row in messages]

    AlarmEventLocation.query.filter_by(event_id=event.id).delete(synchronize_session=False)
    if inbound_ids:
        base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.inbound_id.in_(inbound_ids)).delete(synchronize_session=False)
    events.AlarmEventMessage.query.filter_by(event_id=event.id).delete(synchronize_session=False)
    if inbound_ids:
        base.InboundMessage.query.filter(base.InboundMessage.id.in_(inbound_ids)).delete(synchronize_session=False)
    db.session.delete(event)
    db.session.commit()
    flash("Alarmhændelsen er slettet og indgår ikke længere i statistikken.")
    return redirect(_safe_next(url_for("alarm_statistics")))


@app.post("/alarmer/<int:event_id>/kort")
@base.login_required
def save_alarm_location(event_id: int):
    base.check_csrf()
    event = db.get_or_404(events.AlarmEvent, event_id)
    try:
        latitude = float((request.form.get("latitude") or "").replace(",", "."))
        longitude = float((request.form.get("longitude") or "").replace(",", "."))
    except ValueError:
        flash("Kortpositionen er ugyldig.", "error")
        return redirect(url_for("alarm_event_detail", event_id=event.id))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        flash("Kortpositionen ligger uden for gyldige koordinater.", "error")
        return redirect(url_for("alarm_event_detail", event_id=event.id))

    location = event_location(event.id)
    if location:
        location.latitude = latitude
        location.longitude = longitude
        location.updated_at = base.utcnow()
    else:
        db.session.add(AlarmEventLocation(event_id=event.id, latitude=latitude, longitude=longitude))
    db.session.commit()
    flash("Turen er gemt på alarmkortet.")
    return redirect(url_for("alarm_event_detail", event_id=event.id))


@app.post("/alarmer/<int:event_id>/kort/fjern")
@base.login_required
def remove_alarm_location(event_id: int):
    base.check_csrf()
    event = db.get_or_404(events.AlarmEvent, event_id)
    AlarmEventLocation.query.filter_by(event_id=event.id).delete(synchronize_session=False)
    db.session.commit()
    flash("Turen er fjernet fra alarmkortet.")
    return redirect(url_for("alarm_event_detail", event_id=event.id))


@app.get("/alarmkort")
@base.login_required
def alarm_map():
    rows = (
        db.session.query(AlarmEventLocation, events.AlarmEvent)
        .join(events.AlarmEvent, events.AlarmEvent.id == AlarmEventLocation.event_id)
        .order_by(events.AlarmEvent.started_at.desc())
        .all()
    )
    points = [
        {
            "lat": location.latitude,
            "lon": location.longitude,
            "station": event.station,
            "alarm_type": event.alarm_type,
            "address": event.address,
            "time": events.dk_time(event.started_at),
            "url": url_for("alarm_event_detail", event_id=event.id),
        }
        for location, event in rows
    ]
    return render_template_string(MAP_HTML, title="Alarmkort", points=points)


with app.app_context():
    db.create_all()
