from __future__ import annotations

from flask import flash, redirect, render_template_string, request, url_for

import station_filter_app as previous

app = previous.app
base = previous.base
db = previous.db


class AdminStation(db.Model):
    __tablename__ = "recipient_admin_station"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=base.utcnow)


class RecipientAdminStation(db.Model):
    __tablename__ = "recipient_admin_station_membership"

    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(
        db.Integer,
        db.ForeignKey("recipient.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    station_id = db.Column(
        db.Integer,
        db.ForeignKey("recipient_admin_station.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _clean_station_name(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def station_overview() -> tuple[list[dict], list[base.Recipient]]:
    stations = AdminStation.query.order_by(AdminStation.name.collate("NOCASE")).all()
    recipients = base.Recipient.query.order_by(base.Recipient.name.collate("NOCASE")).all()
    memberships = {
        row.recipient_id: row.station_id
        for row in RecipientAdminStation.query.all()
    }

    grouped: dict[int, list[base.Recipient]] = {station.id: [] for station in stations}
    unassigned: list[base.Recipient] = []

    for recipient in recipients:
        station_id = memberships.get(recipient.id)
        if station_id in grouped:
            grouped[station_id].append(recipient)
        else:
            unassigned.append(recipient)

    return [
        {"station": station, "recipients": grouped[station.id]}
        for station in stations
    ], unassigned


ADMIN_STATIONS_PAGE = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<style>
.station-create{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}
.station-block{margin-bottom:16px;padding:0;overflow:hidden}
.station-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 16px;background:#101a27;border-bottom:1px solid var(--border)}
.station-name{font-weight:800;font-size:18px}.person-row{display:grid;grid-template-columns:minmax(160px,1.4fr) minmax(150px,1fr) 110px minmax(220px,1.2fr);gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid #213044}.person-row:last-child{border-bottom:0}.person-name{font-weight:750}.person-phone{color:var(--muted);font-size:13px}.station-select{width:100%;background:#0d141e;border:1px solid var(--border);color:var(--text);border-radius:10px;padding:9px}.move-form{display:flex;gap:8px;align-items:center}.badge-active{color:#9cf0c4}.badge-inactive{color:#ffb0b0}.hint{margin-top:8px;color:var(--muted);font-size:13px}.unassigned .station-head{background:#2b2412}.unassigned .station-name{color:#ffe49a}
@media(max-width:900px){.person-row{grid-template-columns:1fr}.move-form{align-items:stretch;flex-direction:column}.station-create{grid-template-columns:1fr}}
</style>
<div class="wrap">
  <div class="top">
    <div class="brand"><h1>Personer & stationer</h1><p>SBR Pager · kun administrativt overblik</p></div>
    <div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a><a class="btn" href="{{ url_for('station_filters_page') }}">Alarmfilter</a><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div>
  </div>

  {% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}{% endwith %}

  <div class="grid">
    <section class="card span12">
      <h2>Opret station</h2>
      <form class="station-create" method="post" action="{{ url_for('create_admin_station') }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <div><label>Stationsnavn</label><input name="name" maxlength="120" placeholder="F.eks. Slagelse" required></div>
        <button class="btn primary" type="submit">+ Opret station</button>
      </form>
      <div class="hint">Denne station bruges kun til at gruppere personer i administrationen. Den ændrer ikke, hvilke alarmer personen modtager.</div>
    </section>
  </div>

  {% if unassigned %}
  <h2 style="margin-top:24px">Skal placeres</h2>
  <section class="card span12 station-block unassigned">
    <div class="station-head"><div><div class="station-name">Uden station</div><div class="muted">{{ unassigned|length }} person{{ '' if unassigned|length == 1 else 'er' }}</div></div></div>
    {% for recipient in unassigned %}
    <div class="person-row">
      <div><div class="person-name">{{ recipient.name }}</div><div class="person-phone">{{ recipient.phone }}</div></div>
      <div>{{ 'Aktiv' if recipient.active else 'Inaktiv' }}</div>
      <div class="{{ 'badge-active' if recipient.active else 'badge-inactive' }}">{{ '● Aktiv' if recipient.active else '● Inaktiv' }}</div>
      <form class="move-form" method="post" action="{{ url_for('set_recipient_admin_station', recipient_id=recipient.id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <select class="station-select" name="station_id">
          <option value="" selected>Uden station</option>
          {% for item in station_groups %}<option value="{{ item.station.id }}">{{ item.station.name }}</option>{% endfor %}
        </select>
        <button class="btn small" type="submit">Gem</button>
      </form>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  <h2 style="margin-top:24px">Stationer</h2>
  {% for item in station_groups %}
  <section class="card span12 station-block">
    <div class="station-head"><div><div class="station-name">{{ item.station.name }}</div><div class="muted">{{ item.recipients|length }} person{{ '' if item.recipients|length == 1 else 'er' }}</div></div></div>
    {% if item.recipients %}
      {% for recipient in item.recipients %}
      <div class="person-row">
        <div><div class="person-name">{{ recipient.name }}</div><div class="person-phone">{{ recipient.phone }}</div></div>
        <div>{{ 'Aktiv' if recipient.active else 'Inaktiv' }}</div>
        <div class="{{ 'badge-active' if recipient.active else 'badge-inactive' }}">{{ '● Aktiv' if recipient.active else '● Inaktiv' }}</div>
        <form class="move-form" method="post" action="{{ url_for('set_recipient_admin_station', recipient_id=recipient.id) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <select class="station-select" name="station_id">
            <option value="">Uden station</option>
            {% for candidate in station_groups %}<option value="{{ candidate.station.id }}" {{ 'selected' if candidate.station.id == item.station.id else '' }}>{{ candidate.station.name }}</option>{% endfor %}
          </select>
          <button class="btn small" type="submit">Gem</button>
        </form>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty" style="padding:16px">Ingen personer på stationen endnu.</div>
    {% endif %}
  </section>
  {% else %}
    <section class="card span12"><div class="empty">Der er endnu ikke oprettet nogen administrative stationer.</div></section>
  {% endfor %}
</div>
""",
)


_previous_dashboard = app.view_functions["dashboard"]


def dashboard_with_admin_station_link():
    raw_response = _previous_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response

    html = response.get_data(as_text=True)
    logout_link = f'<a class="btn" href="{url_for("logout")}">Log ud</a>'
    people_link = f'<a class="btn" href="{url_for("admin_stations_page")}">Personer</a>'
    if people_link not in html:
        html = html.replace(logout_link, people_link + logout_link, 1)
    response.set_data(html)
    return response


app.view_functions["dashboard"] = dashboard_with_admin_station_link


@app.get("/personer")
@base.login_required
def admin_stations_page():
    station_groups, unassigned = station_overview()
    return render_template_string(
        ADMIN_STATIONS_PAGE,
        title="Personer & stationer",
        station_groups=station_groups,
        unassigned=unassigned,
    )


@app.post("/personer/stationer")
@base.login_required
def create_admin_station():
    base.check_csrf()
    name = _clean_station_name(request.form.get("name"))
    if len(name) < 2:
        flash("Stationsnavnet skal være mindst 2 tegn.", "error")
        return redirect(url_for("admin_stations_page"))

    existing = AdminStation.query.filter(db.func.lower(AdminStation.name) == name.lower()).first()
    if existing:
        flash("Der findes allerede en station med det navn.", "error")
        return redirect(url_for("admin_stations_page"))

    db.session.add(AdminStation(name=name))
    db.session.commit()
    flash(f"Stationen {name} er oprettet.")
    return redirect(url_for("admin_stations_page"))


@app.post("/personer/<int:recipient_id>/station")
@base.login_required
def set_recipient_admin_station(recipient_id: int):
    base.check_csrf()
    recipient = db.get_or_404(base.Recipient, recipient_id)
    raw_station_id = (request.form.get("station_id") or "").strip()

    membership = RecipientAdminStation.query.filter_by(recipient_id=recipient.id).first()

    if not raw_station_id:
        if membership:
            db.session.delete(membership)
            db.session.commit()
        flash(f"{recipient.name} er flyttet til Uden station.")
        return redirect(url_for("admin_stations_page"))

    try:
        station_id = int(raw_station_id)
    except ValueError:
        flash("Ugyldig station.", "error")
        return redirect(url_for("admin_stations_page"))

    station = db.session.get(AdminStation, station_id)
    if not station:
        flash("Stationen findes ikke længere.", "error")
        return redirect(url_for("admin_stations_page"))

    if membership:
        membership.station_id = station.id
    else:
        db.session.add(RecipientAdminStation(recipient_id=recipient.id, station_id=station.id))
    db.session.commit()

    flash(f"{recipient.name} er placeret på {station.name}.")
    return redirect(url_for("admin_stations_page"))


with app.app_context():
    db.create_all()
