from __future__ import annotations

from collections import Counter
from datetime import timedelta, timezone

from flask import render_template_string, request, url_for

import events_app as events

app = events.app
db = events.db
base = events.base

WEEKDAYS_DA = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
ALLOWED_PERIODS = {30, 90, 365}


def _selected_days() -> int:
    try:
        value = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        value = 30
    return value if value in ALLOWED_PERIODS else 30


def _duration_summary(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(sum(values) / len(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }


def _pct(part: int, total: int) -> float:
    return round(part / total * 100, 1) if total else 0.0


def stats_v2_snapshot(days: int) -> dict:
    now_utc = base.utcnow()
    now_dk = now_utc.astimezone(events.COPENHAGEN)
    start = now_utc - timedelta(days=days)
    period_events = (
        events.AlarmEvent.query.filter(events.AlarmEvent.started_at >= start)
        .order_by(events.AlarmEvent.started_at.asc())
        .all()
    )

    type_counter = Counter()
    station_counter = Counter()
    weekday_counter = Counter({index: 0 for index in range(7)})
    hour_counter = Counter({hour: 0 for hour in range(24)})
    first_delays: list[float] = []
    complete_delays: list[float] = []
    complete_count = 0
    followup_events = 0
    followup_total = 0

    for event in period_events:
        started = events.aware(event.started_at)
        first = events.aware(event.first_alerted_at)
        completed = events.aware(event.completed_at)
        if event.alarm_type:
            type_counter[event.alarm_type] += 1
        if event.station:
            station_counter[event.station] += 1
        if started:
            local = started.astimezone(events.COPENHAGEN)
            weekday_counter[local.weekday()] += 1
            hour_counter[local.hour] += 1
        if started and first:
            first_delays.append(max(0.0, (first - started).total_seconds()))
        if started and completed:
            complete_delays.append(max(0.0, (completed - started).total_seconds()))
        if event.status == "complete":
            complete_count += 1
        if (event.followup_count or 0) > 0:
            followup_events += 1
        followup_total += event.followup_count or 0

    # Daily chart always shows the last 30 local calendar days so it stays
    # readable even when the selected statistics period is 90/365 days.
    today_local = now_dk.date()
    daily_dates = [today_local - timedelta(days=offset) for offset in range(29, -1, -1)]
    daily_counter = Counter({day: 0 for day in daily_dates})
    daily_start_utc = (
        now_dk.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
    ).astimezone(timezone.utc)
    daily_events = events.AlarmEvent.query.filter(events.AlarmEvent.started_at >= daily_start_utc).all()
    for event in daily_events:
        started = events.aware(event.started_at)
        if not started:
            continue
        day = started.astimezone(events.COPENHAGEN).date()
        if day in daily_counter:
            daily_counter[day] += 1

    daily = [
        {
            "date": day.strftime("%d/%m"),
            "long_date": day.strftime("%d/%m/%Y"),
            "count": daily_counter[day],
        }
        for day in daily_dates
    ]
    max_daily = max((item["count"] for item in daily), default=0) or 1

    weekdays = [
        {"name": WEEKDAYS_DA[index], "count": weekday_counter[index]}
        for index in range(7)
    ]
    hours = [
        {"name": f"{hour:02d}–{(hour + 1) % 24:02d}", "count": hour_counter[hour]}
        for hour in range(24)
    ]
    max_weekday = max((item["count"] for item in weekdays), default=0) or 1
    max_hour = max((item["count"] for item in hours), default=0) or 1
    max_type = max(type_counter.values(), default=0) or 1
    max_station = max(station_counter.values(), default=0) or 1

    peak_weekday = max(weekdays, key=lambda item: item["count"]) if period_events else None
    peak_hour = max(hours, key=lambda item: item["count"]) if period_events else None

    return {
        "days": days,
        "total": len(period_events),
        "complete": complete_count,
        "complete_pct": _pct(complete_count, len(period_events)),
        "followup_events": followup_events,
        "followup_total": followup_total,
        "followup_pct": _pct(followup_events, len(period_events)),
        "first": _duration_summary(first_delays),
        "complete_time": _duration_summary(complete_delays),
        "daily": daily,
        "max_daily": max_daily,
        "weekdays": weekdays,
        "max_weekday": max_weekday,
        "hours": hours,
        "max_hour": max_hour,
        "types": type_counter.most_common(10),
        "max_type": max_type,
        "stations": station_counter.most_common(10),
        "max_station": max_station,
        "peak_weekday": peak_weekday,
        "peak_hour": peak_hour,
    }


STATISTICS_V2_HTML = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<style>
.span3{grid-column:span 3}.span8{grid-column:span 8}.span4{grid-column:span 4}
.periods{display:flex;gap:7px;flex-wrap:wrap}.periods .btn.active{background:#1d6a49;border-color:#2a9167}
.kpi-sub{margin:6px 0 0;color:var(--muted);font-size:13px}.metric.small{font-size:22px}
.hbar{display:grid;grid-template-columns:minmax(90px,170px) 1fr 42px;gap:10px;align-items:center;margin:9px 0}.hlabel{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.htrack{height:10px;background:#0d141e;border-radius:99px;overflow:hidden}.hfill{height:100%;background:var(--green);border-radius:99px}.hvalue{text-align:right;font-variant-numeric:tabular-nums}
.vchart{height:245px;display:flex;align-items:flex-end;gap:4px;padding:20px 0 24px;border-bottom:1px solid var(--border)}.vitem{flex:1;min-width:5px;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;position:relative}.vbar{width:100%;max-width:22px;min-height:2px;background:var(--green);border-radius:5px 5px 2px 2px}.vlabel{position:absolute;bottom:-20px;font-size:9px;color:var(--muted);white-space:nowrap}.vcount{font-size:10px;color:var(--muted);margin-bottom:3px}
.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.summarybox{background:#0d141e;border:1px solid var(--border);border-radius:12px;padding:12px}.summarybox strong{display:block;font-size:20px;margin-top:4px}.summarybox span{font-size:12px;color:var(--muted)}
.section-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}.section-head h2{margin:0}.section-head p{margin:4px 0 0}
@media(max-width:880px){.span3,.span4,.span8{grid-column:span 12}.summary{grid-template-columns:1fr}.vchart{overflow-x:auto}.vitem{min-width:14px}}
</style>
<div class="wrap">
  <div class="top">
    <div class="brand"><h1>Alarmstatistik</h1><p>SBR Pager · hændelser, mønstre og leveringstider</p></div>
    <div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div>
  </div>

  <div class="section-head"><div><h2>Periode</h2><p class="muted">Vælg grundlag for fordelinger og nøgletal.</p></div><div class="periods">
    {% for option in [30,90,365] %}<a class="btn small {{ 'active' if stats.days == option else '' }}" href="{{ url_for('alarm_statistics', days=option) }}">{{ option }} dage</a>{% endfor %}
  </div></div>

  <div class="grid">
    <section class="card span3"><h2>Alarmer</h2><div class="metric">{{ stats.total }}</div><p class="kpi-sub">Seneste {{ stats.days }} dage</p></section>
    <section class="card span3"><h2>Komplette</h2><div class="metric">{{ stats.complete_pct }}%</div><p class="kpi-sub">{{ stats.complete }} af {{ stats.total }} hændelser</p></section>
    <section class="card span3"><h2>Med Sending 2</h2><div class="metric">{{ stats.followup_pct }}%</div><p class="kpi-sub">{{ stats.followup_events }} hændelser · {{ stats.followup_total }} opfølgninger</p></section>
    <section class="card span3"><h2>Travleste tidspunkt</h2><div class="metric small">{{ stats.peak_hour.name if stats.peak_hour else '—' }}</div><p class="kpi-sub">{{ stats.peak_weekday.name if stats.peak_weekday else 'Ikke nok data' }}</p></section>

    <section class="card span6"><h2>Første WhatsApp-varsling</h2><div class="summary"><div class="summarybox"><span>Gennemsnit</span><strong>{{ (stats.first.avg|string + ' s') if stats.first.avg is not none else '—' }}</strong></div><div class="summarybox"><span>Hurtigste</span><strong>{{ (stats.first.min|string + ' s') if stats.first.min is not none else '—' }}</strong></div><div class="summarybox"><span>Langsomste</span><strong>{{ (stats.first.max|string + ' s') if stats.first.max is not none else '—' }}</strong></div></div></section>
    <section class="card span6"><h2>Komplet alarmmelding</h2><div class="summary"><div class="summarybox"><span>Gennemsnit</span><strong>{{ (stats.complete_time.avg|string + ' s') if stats.complete_time.avg is not none else '—' }}</strong></div><div class="summarybox"><span>Hurtigste</span><strong>{{ (stats.complete_time.min|string + ' s') if stats.complete_time.min is not none else '—' }}</strong></div><div class="summarybox"><span>Langsomste</span><strong>{{ (stats.complete_time.max|string + ' s') if stats.complete_time.max is not none else '—' }}</strong></div></div></section>

    <section class="card span12"><div class="section-head"><div><h2>Alarmer pr. dag</h2><p class="muted">Seneste 30 kalenderdage, uanset valgt statistikperiode.</p></div></div><div class="vchart">{% for item in stats.daily %}<div class="vitem" title="{{ item.long_date }} · {{ item.count }} alarm(er)"><div class="vcount">{{ item.count if item.count else '' }}</div><div class="vbar" style="height:{{ [4, (item.count / stats.max_daily * 100)|round]|max }}%"></div>{% if loop.index0 % 5 == 0 or loop.last %}<div class="vlabel">{{ item.date }}</div>{% endif %}</div>{% endfor %}</div></section>

    <section class="card span6"><h2>Ugedage · {{ stats.days }} dage</h2>{% for item in stats.weekdays %}<div class="hbar"><div class="hlabel">{{ item.name }}</div><div class="htrack"><div class="hfill" style="width:{{ (item.count / stats.max_weekday * 100)|round }}%"></div></div><div class="hvalue">{{ item.count }}</div></div>{% endfor %}</section>
    <section class="card span6"><h2>Tidspunkt på døgnet · {{ stats.days }} dage</h2>{% for item in stats.hours %}<div class="hbar"><div class="hlabel">{{ item.name }}</div><div class="htrack"><div class="hfill" style="width:{{ (item.count / stats.max_hour * 100)|round }}%"></div></div><div class="hvalue">{{ item.count }}</div></div>{% endfor %}</section>

    <section class="card span6"><h2>Alarmtyper · {{ stats.days }} dage</h2>{% for name,count in stats.types %}<div class="hbar"><div class="hlabel" title="{{ name }}">{{ name }}</div><div class="htrack"><div class="hfill" style="width:{{ (count / stats.max_type * 100)|round }}%"></div></div><div class="hvalue">{{ count }}</div></div>{% else %}<div class="empty">Ikke nok data endnu.</div>{% endfor %}</section>
    <section class="card span6"><h2>Stationer · {{ stats.days }} dage</h2>{% for name,count in stats.stations %}<div class="hbar"><div class="hlabel">{{ name }}</div><div class="htrack"><div class="hfill" style="width:{{ (count / stats.max_station * 100)|round }}%"></div></div><div class="hvalue">{{ count }}</div></div>{% else %}<div class="empty">Ikke nok data endnu.</div>{% endfor %}</section>

    <section class="card span12"><div class="section-head"><div><h2>Alarmhændelser</h2><p class="muted">Én række pr. hændelse. Pre-alert, komplet melding og Sending 2 ligger i tidslinjen.</p></div></div><div class="tablewrap"><table><thead><tr><th>Tid</th><th>Station</th><th>Alarmtype</th><th>Adresse</th><th>Status</th><th>Sending 2</th><th></th></tr></thead><tbody>{% for event in alarm_events %}<tr><td>{{ dk_time(event.started_at) }}</td><td>{{ event.station or '—' }}</td><td>{{ event.alarm_type or '—' }}</td><td class="bodycell">{{ event.address or '—' }}</td><td><span class="tag">{{ 'Komplet' if event.status == 'complete' else 'Afventer' }}</span></td><td>{{ event.followup_count or 0 }}</td><td><a class="btn small" href="{{ url_for('alarm_event_detail', event_id=event.id) }}">Vis tidslinje</a></td></tr>{% else %}<tr><td colspan="7" class="empty">Ingen hændelser i perioden.</td></tr>{% endfor %}</tbody></table></div></section>
  </div>
  <div class="footer">Statistikken bygger kun på registrerede SBR Pager-hændelser. Tider vises i Europe/Copenhagen.</div>
</div>
""",
)


def alarm_statistics_v2():
    days = _selected_days()
    start = base.utcnow() - timedelta(days=days)
    alarm_events = (
        events.AlarmEvent.query.filter(events.AlarmEvent.started_at >= start)
        .order_by(events.AlarmEvent.started_at.desc())
        .limit(250)
        .all()
    )
    return render_template_string(
        STATISTICS_V2_HTML,
        title="Alarmstatistik",
        stats=stats_v2_snapshot(days),
        alarm_events=alarm_events,
    )


alarm_statistics_v2 = base.login_required(alarm_statistics_v2)
app.view_functions["alarm_statistics"] = alarm_statistics_v2


_previous_dashboard = app.view_functions["dashboard"]


def dashboard_v2():
    raw_response = _previous_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response
    html = response.get_data(as_text=True)
    html = html.replace(
        "<h2>Seneste SMS'er</h2>",
        "<h2>Teknisk SMS-log</h2><p class=\"muted\" style=\"margin-top:-6px\">Rå pre-alerts, komplette SMS'er og opfølgninger til fejlsøgning.</p>",
        1,
    )
    html = html.replace(
        "<h2>Seneste WhatsApp-leveringer</h2>",
        "<h2>Teknisk WhatsApp-log</h2>",
        1,
    )
    response.set_data(html)
    return response


app.view_functions["dashboard"] = dashboard_v2
