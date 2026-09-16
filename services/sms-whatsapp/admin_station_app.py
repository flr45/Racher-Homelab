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


def alarm_filter_map() -> dict[int, set[str]]:
    recipients = base.Recipient.query.order_by(base.Recipient.name.collate("NOCASE")).all()
    return {
        recipient.id: previous.configured_stations(recipient.id)
        for recipient in recipients
    }


ADMIN_USERS_PAGE = base.BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<style>
.station-create{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}
.station-block{margin-bottom:16px;padding:0;overflow:hidden}
.station-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 16px;background:#101a27;border-bottom:1px solid var(--border)}
.station-name{font-weight:800;font-size:18px}.unassigned .station-head{background:#2b2412}.unassigned .station-name{color:#ffe49a}
.user-card{border-bottom:1px solid #213044}.user-card:last-child{border-bottom:0}.user-card>summary{list-style:none;cursor:pointer;padding:14px 16px}.user-card>summary::-webkit-details-marker{display:none}.user-card[open]>summary{background:#0e1722}
.user-summary{display:grid;grid-template-columns:minmax(170px,1.4fr) 110px minmax(150px,1fr) 34px;gap:12px;align-items:center}.user-name{font-weight:780}.user-phone{color:var(--muted);font-size:13px}.badge-active{color:#9cf0c4}.badge-inactive{color:#ffb0b0}.user-arrow{font-size:18px;color:var(--muted);transition:transform .18s ease;text-align:right}.user-card[open] .user-arrow{transform:rotate(180deg)}
.user-editor{padding:0 16px 16px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.editor-box{background:#0d141e;border:1px solid var(--border);border-radius:13px;padding:14px}.editor-box h3{font-size:14px;margin:0 0 10px}.editor-row{display:grid;grid-template-columns:1fr 1fr;gap:9px}.editor-box label{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}.editor-box input,.editor-box select{width:100%;background:#09111a;border:1px solid var(--border);color:var(--text);border-radius:10px;padding:9px 10px}.active-choice{display:flex!important;align-items:center;gap:8px;margin:10px 0 0!important;color:var(--text)!important;font-size:13px!important}.active-choice input{width:auto;margin:0}.editor-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:11px;flex-wrap:wrap}
.alarm-box{grid-column:1/-1}.alarm-grid{display:grid;grid-template-columns:repeat(7,minmax(52px,1fr));gap:7px}.alarm-choice{display:flex!important;align-items:center;justify-content:center;gap:5px;background:#101a27;border:1px solid var(--border);border-radius:9px;padding:8px 5px;color:var(--text)!important;font-size:12px!important;cursor:pointer}.alarm-choice input{width:auto;margin:0}.alarm-summary{color:var(--muted);font-size:12px}.hint{margin-top:8px;color:var(--muted);font-size:13px}.danger-zone{grid-column:1/-1;border-color:#60313a;background:#24151a}.danger-layout{display:flex;justify-content:space-between;align-items:center;gap:15px}.danger-text{color:#ffb0b0;font-size:13px}
@media(max-width:900px){.user-summary{grid-template-columns:1fr auto}.user-summary .alarm-summary{grid-column:1/-1}.user-editor{grid-template-columns:1fr}.editor-row{grid-template-columns:1fr}.station-create{grid-template-columns:1fr}.alarm-grid{grid-template-columns:repeat(4,1fr)}.alarm-box,.danger-zone{grid-column:auto}.danger-layout{align-items:flex-start;flex-direction:column}}
</style>

{% macro user_card(recipient, current_station_id) -%}
  {% set selected = alarm_filters[recipient.id] %}
  <details class="user-card">
    <summary>
      <div class="user-summary">
        <div><div class="user-name">{{ recipient.name }}</div><div class="user-phone">{{ recipient.phone }}</div></div>
        <div class="{{ 'badge-active' if recipient.active else 'badge-inactive' }}">{{ '● Aktiv' if recipient.active else '● Inaktiv' }}</div>
        <div class="alarm-summary">Modtager: <strong>{{ 'Alle stationer' if '*' in selected else (selected|sort|join(', ')) }}</strong></div>
        <div class="user-arrow">⌄</div>
      </div>
    </summary>

    <div class="user-editor">
      <section class="editor-box">
        <h3>Redigér bruger</h3>
        <form method="post" action="{{ url_for('update_admin_user', recipient_id=recipient.id) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <div class="editor-row">
            <div><label>Navn</label><input name="name" value="{{ recipient.name }}" maxlength="120" required></div>
            <div><label>WhatsApp-nummer</label><input name="phone" value="{{ recipient.phone }}" required></div>
          </div>
          <label class="active-choice"><input type="checkbox" name="active" value="1" {{ 'checked' if recipient.active else '' }}> Aktiv modtager</label>
          <div class="editor-actions"><button class="btn primary small" type="submit">Gem bruger</button></div>
        </form>
      </section>

      <section class="editor-box">
        <h3>Administrativ station</h3>
        <form method="post" action="{{ url_for('set_recipient_admin_station', recipient_id=recipient.id) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <label>Placering i brugeroversigten</label>
          <select name="station_id">
            <option value="" {{ 'selected' if not current_station_id else '' }}>Uden station</option>
            {% for item in station_groups %}<option value="{{ item.station.id }}" {{ 'selected' if current_station_id == item.station.id else '' }}>{{ item.station.name }}</option>{% endfor %}
          </select>
          <div class="editor-actions"><button class="btn small" type="submit">Gem placering</button></div>
        </form>
      </section>

      <section class="editor-box alarm-box">
        <h3>Modtagerstationer</h3>
        <div class="hint" style="margin:0 0 10px">Vælg hvilke stationers alarmer brugeren skal modtage. Dette påvirker ikke den administrative station ovenfor.</div>
        <form class="alarm-filter-form" method="post" action="{{ url_for('update_admin_user_alarm_stations', recipient_id=recipient.id) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <div class="alarm-grid">
            <label class="alarm-choice"><input type="checkbox" name="stations" value="*" {{ 'checked' if '*' in selected else '' }}>Alle</label>
            {% for code in alarm_station_codes %}<label class="alarm-choice"><input type="checkbox" name="stations" value="{{ code }}" {{ 'checked' if code in selected else '' }}>{{ code }}</label>{% endfor %}
          </div>
          <div class="editor-actions"><button class="btn primary small" type="submit">Gem modtagerstationer</button></div>
        </form>
      </section>

      <section class="editor-box danger-zone">
        <div class="danger-layout">
          <div><h3>Slet bruger</h3><div class="danger-text">Brugeren fjernes som modtager samt fra station og alarmfilter. Historiske leveringslogs bevares.</div></div>
          <form method="post" action="{{ url_for('delete_admin_user', recipient_id=recipient.id) }}" onsubmit="return confirm('Er du sikker på, at {{ recipient.name }} skal slettes?')">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <button class="btn danger small" type="submit">Slet bruger</button>
          </form>
        </div>
      </section>
    </div>
  </details>
{%- endmacro %}

<div class="wrap">
  <div class="top">
    <div class="brand"><h1>Brugere & stationer</h1><p>SBR Pager · administration af modtagere</p></div>
    <div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">← Administration</a><a class="btn" href="{{ url_for('station_filters_page') }}">Alarmfilter</a><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div>
  </div>

  {% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}{% endwith %}

  <div class="grid">
    <section class="card span12">
      <h2>Opret administrativ station</h2>
      <form class="station-create" method="post" action="{{ url_for('create_admin_station') }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <div><label>Stationsnavn</label><input name="name" maxlength="120" placeholder="F.eks. Slagelse" required></div>
        <button class="btn primary" type="submit">+ Opret station</button>
      </form>
      <div class="hint">Stationen bruges kun til at gruppere brugerne i administrationen. Hvilke alarmer de modtager vælges separat på hver bruger.</div>
    </section>
  </div>

  {% if unassigned %}
  <h2 style="margin-top:24px">Skal placeres</h2>
  <section class="card span12 station-block unassigned">
    <div class="station-head"><div><div class="station-name">Uden station</div><div class="muted">{{ unassigned|length }} bruger{{ '' if unassigned|length == 1 else 'e' }}</div></div></div>
    {% for recipient in unassigned %}{{ user_card(recipient, none) }}{% endfor %}
  </section>
  {% endif %}

  <h2 style="margin-top:24px">Stationer</h2>
  {% for item in station_groups %}
  <section class="card span12 station-block">
    <div class="station-head"><div><div class="station-name">{{ item.station.name }}</div><div class="muted">{{ item.recipients|length }} bruger{{ '' if item.recipients|length == 1 else 'e' }}</div></div></div>
    {% if item.recipients %}
      {% for recipient in item.recipients %}{{ user_card(recipient, item.station.id) }}{% endfor %}
    {% else %}
      <div class="empty" style="padding:16px">Ingen brugere på stationen endnu.</div>
    {% endif %}
  </section>
  {% else %}
    <section class="card span12"><div class="empty">Der er endnu ikke oprettet administrative stationer.</div></section>
  {% endfor %}
</div>
<script>
document.querySelectorAll('.alarm-filter-form').forEach(function(form){
  const all = form.querySelector('input[value="*"]');
  const specific = Array.from(form.querySelectorAll('input[name="stations"]')).filter(function(input){ return input.value !== '*'; });
  if (!all) return;
  all.addEventListener('change', function(){ if (all.checked) specific.forEach(function(input){ input.checked = false; }); });
  specific.forEach(function(input){ input.addEventListener('change', function(){ if (input.checked) all.checked = false; }); });
});
</script>
""",
)


_previous_dashboard = app.view_functions["dashboard"]


def dashboard_with_admin_user_link():
    raw_response = _previous_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response

    html = response.get_data(as_text=True)
    logout_link = f'<a class="btn" href="{url_for("logout")}">Log ud</a>'
    user_link = f'<a class="btn" href="{url_for("admin_users_page")}">Brugere</a>'
    if user_link not in html:
        html = html.replace(logout_link, user_link + logout_link, 1)
    response.set_data(html)
    return response


app.view_functions["dashboard"] = dashboard_with_admin_user_link


@app.get("/brugere")
@base.login_required
def admin_users_page():
    station_groups, unassigned = station_overview()
    return render_template_string(
        ADMIN_USERS_PAGE,
        title="Brugere & stationer",
        station_groups=station_groups,
        unassigned=unassigned,
        alarm_filters=alarm_filter_map(),
        alarm_station_codes=previous.STATIONS,
    )


@app.get("/personer")
@base.login_required
def old_personer_redirect():
    return redirect(url_for("admin_users_page"))


@app.post("/brugere/stationer")
@base.login_required
def create_admin_station():
    base.check_csrf()
    name = _clean_station_name(request.form.get("name"))
    if len(name) < 2:
        flash("Stationsnavnet skal være mindst 2 tegn.", "error")
        return redirect(url_for("admin_users_page"))

    existing = AdminStation.query.filter(db.func.lower(AdminStation.name) == name.lower()).first()
    if existing:
        flash("Der findes allerede en station med det navn.", "error")
        return redirect(url_for("admin_users_page"))

    db.session.add(AdminStation(name=name))
    db.session.commit()
    flash(f"Stationen {name} er oprettet.")
    return redirect(url_for("admin_users_page"))


@app.post("/brugere/<int:recipient_id>/station")
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
        return redirect(url_for("admin_users_page"))

    try:
        station_id = int(raw_station_id)
    except ValueError:
        flash("Ugyldig station.", "error")
        return redirect(url_for("admin_users_page"))

    station = db.session.get(AdminStation, station_id)
    if not station:
        flash("Stationen findes ikke længere.", "error")
        return redirect(url_for("admin_users_page"))

    if membership:
        membership.station_id = station.id
    else:
        db.session.add(RecipientAdminStation(recipient_id=recipient.id, station_id=station.id))
    db.session.commit()
    flash(f"{recipient.name} er placeret på {station.name}.")
    return redirect(url_for("admin_users_page"))


@app.post("/brugere/<int:recipient_id>/rediger")
@base.login_required
def update_admin_user(recipient_id: int):
    base.check_csrf()
    recipient = db.get_or_404(base.Recipient, recipient_id)
    try:
        name = " ".join((request.form.get("name") or "").strip().split())
        if not name:
            raise ValueError("Navn mangler.")
        phone = base.normalize_phone(request.form.get("phone", ""))
        duplicate = base.Recipient.query.filter(
            base.Recipient.phone == phone,
            base.Recipient.id != recipient.id,
        ).first()
        if duplicate:
            raise ValueError("Telefonnummeret bruges allerede af en anden bruger.")
        recipient.name = name
        recipient.phone = phone
        recipient.active = request.form.get("active") == "1"
        db.session.commit()
        flash(f"{recipient.name} er opdateret.")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin_users_page"))


@app.post("/brugere/<int:recipient_id>/alarmstationer")
@base.login_required
def update_admin_user_alarm_stations(recipient_id: int):
    base.check_csrf()
    recipient = db.get_or_404(base.Recipient, recipient_id)
    selected = {value.upper() for value in request.form.getlist("stations")}
    selected &= set(previous.STATIONS) | {previous.ALL_STATIONS}
    if previous.ALL_STATIONS in selected or not selected:
        selected = {previous.ALL_STATIONS}

    previous.RecipientStationFilter.query.filter_by(recipient_id=recipient.id).delete()
    for station in sorted(selected):
        db.session.add(previous.RecipientStationFilter(recipient_id=recipient.id, station=station))
    db.session.commit()

    label = "Alle stationer" if previous.ALL_STATIONS in selected else ", ".join(sorted(selected))
    flash(f"Modtagerstationer for {recipient.name}: {label}.")
    return redirect(url_for("admin_users_page"))


@app.post("/brugere/<int:recipient_id>/slet")
@base.login_required
def delete_admin_user(recipient_id: int):
    base.check_csrf()
    recipient = db.get_or_404(base.Recipient, recipient_id)
    name = recipient.name

    RecipientAdminStation.query.filter_by(recipient_id=recipient.id).delete()
    previous.RecipientStationFilter.query.filter_by(recipient_id=recipient.id).delete()
    db.session.delete(recipient)
    db.session.commit()

    flash(f"{name} er slettet.")
    return redirect(url_for("admin_users_page"))


with app.app_context():
    db.create_all()
