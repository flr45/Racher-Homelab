from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{6,14}$")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:////data/sms-whatsapp.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SMS_WHATSAPP_SESSION_SECRET", "") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SMS_WHATSAPP_COOKIE_SECURE", "false").lower() == "true"
db = SQLAlchemy(app)


class AllowedSender(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: utcnow())


class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: utcnow())


class InboundMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.String(128), unique=True, nullable=False, index=True)
    sender = db.Column(db.String(20), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False)
    accepted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: utcnow())


class WhatsAppDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id"), nullable=True, index=True)
    recipient_name = db.Column(db.String(120), nullable=False)
    recipient_phone = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, index=True)
    message_id = db.Column(db.String(255))
    error = db.Column(db.Text)
    attempted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: utcnow())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s().-]", "", value or "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not PHONE_PATTERN.fullmatch(phone):
        raise ValueError("Ugyldigt telefonnummer")
    if not phone.startswith("+"):
        phone = "+" + phone
    return phone


def parse_received_at(value: str | None) -> datetime:
    if not value:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Ugyldigt modtagelsestidspunkt") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def make_source_id(sender: str, body: str, received_at: datetime, supplied: str | None) -> str:
    if supplied and str(supplied).strip():
        return str(supplied).strip()[:128]
    digest = hashlib.sha256(
        f"{sender}|{received_at.isoformat()}|{body}".encode("utf-8")
    ).hexdigest()
    return f"sms:{digest}"


def admin_username() -> str:
    return os.getenv("SMS_WHATSAPP_ADMIN_USERNAME", "admin").strip() or "admin"


def password_matches(candidate: str) -> bool:
    expected = os.getenv("SMS_WHATSAPP_ADMIN_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(candidate, expected)


def logged_in() -> bool:
    return bool(session.get("admin_authenticated"))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not logged_in():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def check_csrf() -> None:
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        abort(400, "Ugyldig CSRF-token")


app.jinja_env.globals["csrf_token"] = csrf_token


def openwa_base_url() -> str:
    base = os.getenv("OPENWA_BASE_URL", "http://openwa:2785/api").strip().rstrip("/")
    if not base.endswith("/api"):
        base += "/api"
    return base


def openwa_request(path: str, method: str = "GET", payload: dict | None = None, timeout: int = 12):
    api_key = os.getenv("OPENWA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENWA_API_KEY mangler")
    data = None
    headers = {"X-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{openwa_base_url()}/{path.lstrip('/')}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenWA HTTP {exc.code}: {details[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenWA kan ikke kontaktes: {exc.reason}") from exc


def openwa_session_id() -> str:
    value = os.getenv("OPENWA_SESSION_ID", "").strip()
    if not value:
        raise RuntimeError("OPENWA_SESSION_ID mangler")
    return value


def phone_to_chat_id(phone: str) -> str:
    return re.sub(r"\D", "", normalize_phone(phone)) + "@c.us"


def send_whatsapp(phone: str, message: str) -> str | None:
    body = (message or "").strip()
    if not body:
        raise ValueError("Beskeden er tom")
    result = openwa_request(
        f"sessions/{openwa_session_id()}/messages/send-text",
        method="POST",
        payload={"chatId": phone_to_chat_id(phone), "text": body[:4096]},
    )
    return (result or {}).get("messageId")


def openwa_status() -> dict:
    try:
        session_id = openwa_session_id()
        sessions = openwa_request("sessions")
        if not isinstance(sessions, list):
            return {"state": "unknown", "detail": "Uventet svar fra OpenWA"}
        match = next((item for item in sessions if str(item.get("id")) == session_id), None)
        if not match:
            return {"state": "missing", "detail": "Sessionen findes ikke"}
        status = str(match.get("status") or "unknown").lower()
        return {"state": status, "detail": match.get("name") or session_id}
    except Exception as exc:  # noqa: BLE001
        return {"state": "offline", "detail": str(exc)[:180]}


def require_ingest_token() -> None:
    configured = os.getenv("SMS_WHATSAPP_INGEST_TOKEN", "").strip()
    if not configured:
        abort(503, "SMS_WHATSAPP_INGEST_TOKEN mangler")
    authorization = request.headers.get("Authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), configured):
        abort(401)


def deliver_inbound(inbound: InboundMessage) -> tuple[int, int]:
    recipients = Recipient.query.filter_by(active=True).order_by(Recipient.name).all()
    sent = 0
    failed = 0
    for recipient in recipients:
        delivery = WhatsAppDelivery(
            inbound_id=inbound.id,
            recipient_name=recipient.name,
            recipient_phone=recipient.phone,
            status="pending",
            attempted_at=utcnow(),
        )
        db.session.add(delivery)
        db.session.commit()
        try:
            delivery.message_id = send_whatsapp(recipient.phone, inbound.body)
            delivery.status = "sent"
            delivery.error = None
            sent += 1
        except Exception as exc:  # noqa: BLE001
            delivery.status = "failed"
            delivery.error = str(exc)[:1000]
            failed += 1
        delivery.attempted_at = utcnow()
        db.session.commit()
    return sent, failed


BASE_HTML = r"""
<!doctype html>
<html lang="da">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ title }} · SMS → WhatsApp</title>
  <style>
    :root{color-scheme:dark;--bg:#0b0f14;--panel:#121923;--panel2:#182231;--border:#263447;--text:#eef4fb;--muted:#98a9bd;--green:#39d98a;--red:#ff6b6b;--yellow:#ffd166;--blue:#66a3ff}
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(180deg,#081019,#0b0f14 35%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    a{color:inherit}.wrap{max-width:1220px;margin:0 auto;padding:28px 18px 60px}.top{display:flex;gap:16px;align-items:center;justify-content:space-between;margin-bottom:22px}.brand h1{font-size:28px;margin:0}.brand p{margin:5px 0 0;color:var(--muted)}
    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}.card{background:rgba(18,25,35,.94);border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 14px 40px rgba(0,0,0,.18)}.span4{grid-column:span 4}.span6{grid-column:span 6}.span12{grid-column:span 12}
    .metric{font-size:26px;font-weight:750}.muted{color:var(--muted)}.status{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border-radius:999px;background:#0c141d;border:1px solid var(--border);font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}.ok .dot{background:var(--green)}.bad .dot{background:var(--red)}.warn .dot{background:var(--yellow)}
    h2{font-size:17px;margin:0 0 14px} table{width:100%;border-collapse:collapse}th,td{padding:11px 8px;border-bottom:1px solid #213044;text-align:left;vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}tr:last-child td{border-bottom:0}.bodycell{max-width:520px;white-space:pre-wrap;word-break:break-word}
    input{width:100%;background:#0d141e;border:1px solid var(--border);color:var(--text);border-radius:11px;padding:10px 11px;outline:none}input:focus{border-color:var(--blue)}.formrow{display:grid;grid-template-columns:1fr 1fr auto;gap:9px;align-items:end}.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--border);background:#1c2a3a;color:var(--text);padding:9px 12px;border-radius:10px;cursor:pointer;text-decoration:none;font-weight:650}.btn:hover{filter:brightness(1.12)}.btn.primary{background:#1d6a49;border-color:#2a9167}.btn.danger{background:#54252b;border-color:#8d3945}.btn.small{padding:6px 9px;font-size:12px}.actions{display:flex;gap:6px;flex-wrap:wrap}.inline{display:inline}.flash{padding:11px 13px;border-radius:11px;background:#16283a;border:1px solid #2b4966;margin-bottom:12px}.flash.error{background:#3a181c;border-color:#6b2d35}.tag{font-size:12px;padding:4px 8px;border-radius:999px;border:1px solid var(--border)}
    .login{max-width:430px;margin:10vh auto}.login .card{padding:26px}.login input{margin:5px 0 14px}.login .btn{width:100%}.footer{margin-top:25px;color:var(--muted);font-size:12px}.empty{padding:18px 0;color:var(--muted)}
    @media(max-width:880px){.span4,.span6{grid-column:span 12}.formrow{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.tablewrap{overflow-x:auto}}
  </style>
</head>
<body>
{% block content %}{% endblock %}
</body>
</html>
"""

LOGIN_HTML = BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<div class="wrap login"><div class="card">
  <div class="brand"><h1>SMS → WhatsApp</h1><p>Administration</p></div>
  <br>
  {% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}{% endwith %}
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Brugernavn</label><input name="username" autocomplete="username" required>
    <label>Adgangskode</label><input type="password" name="password" autocomplete="current-password" required>
    <button class="btn primary" type="submit">Log ind</button>
  </form>
</div></div>
""",
)

DASHBOARD_HTML = BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    r"""
<div class="wrap">
  <div class="top"><div class="brand"><h1>SMS → WhatsApp</h1><p>Racher gateway · SMS-modem → OpenWA</p></div><div class="actions"><form method="post" action="{{ url_for('test_message') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn primary">Send testbesked</button></form><a class="btn" href="{{ url_for('logout') }}">Log ud</a></div></div>
  {% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}{% endwith %}
  <div class="grid">
    <section class="card span4"><h2>OpenWA</h2><div class="status {{ 'ok' if openwa.state == 'ready' else 'warn' if openwa.state not in ['offline','missing'] else 'bad' }}"><span class="dot"></span>{{ openwa.state }}</div><p class="muted">{{ openwa.detail }}</p></section>
    <section class="card span4"><h2>Aktive afsendere</h2><div class="metric">{{ sender_count }}</div><p class="muted">SMS-numre der må videresendes.</p></section>
    <section class="card span4"><h2>Aktive modtagere</h2><div class="metric">{{ recipient_count }}</div><p class="muted">WhatsApp-brugere der modtager SMS'en.</p></section>

    <section class="card span6"><h2>Godkendte SMS-afsendere</h2>
      <form class="formrow" method="post" action="{{ url_for('create_sender') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div><label class="muted">Navn</label><input name="name" placeholder="Alarmcentral" required></div><div><label class="muted">Telefonnummer</label><input name="phone" placeholder="+4512345678" required></div><button class="btn primary">Tilføj</button></form>
      <div class="tablewrap"><table><thead><tr><th>Navn</th><th>Nummer</th><th>Status</th><th></th></tr></thead><tbody>{% for item in senders %}<tr><td>{{ item.name }}</td><td>{{ item.phone }}</td><td><span class="tag">{{ 'Aktiv' if item.active else 'Pause' }}</span></td><td><div class="actions"><form class="inline" method="post" action="{{ url_for('toggle_sender', sender_id=item.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn small">{{ 'Deaktivér' if item.active else 'Aktivér' }}</button></form><form class="inline" method="post" action="{{ url_for('delete_sender', sender_id=item.id) }}" onsubmit="return confirm('Slet dette nummer?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn small danger">Slet</button></form></div></td></tr>{% else %}<tr><td colspan="4" class="empty">Ingen godkendte afsendere endnu.</td></tr>{% endfor %}</tbody></table></div>
    </section>

    <section class="card span6"><h2>WhatsApp-modtagere</h2>
      <form class="formrow" method="post" action="{{ url_for('create_recipient') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div><label class="muted">Navn</label><input name="name" placeholder="Frederik" required></div><div><label class="muted">WhatsApp-nummer</label><input name="phone" placeholder="+4512345678" required></div><button class="btn primary">Tilføj</button></form>
      <div class="tablewrap"><table><thead><tr><th>Navn</th><th>Nummer</th><th>Status</th><th></th></tr></thead><tbody>{% for item in recipients %}<tr><td>{{ item.name }}</td><td>{{ item.phone }}</td><td><span class="tag">{{ 'Aktiv' if item.active else 'Pause' }}</span></td><td><div class="actions"><form class="inline" method="post" action="{{ url_for('toggle_recipient', recipient_id=item.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn small">{{ 'Deaktivér' if item.active else 'Aktivér' }}</button></form><form class="inline" method="post" action="{{ url_for('delete_recipient', recipient_id=item.id) }}" onsubmit="return confirm('Slet denne modtager?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn small danger">Slet</button></form></div></td></tr>{% else %}<tr><td colspan="4" class="empty">Ingen WhatsApp-modtagere endnu.</td></tr>{% endfor %}</tbody></table></div>
    </section>

    <section class="card span12"><h2>Seneste SMS'er</h2><div class="tablewrap"><table><thead><tr><th>Tid</th><th>Afsender</th><th>Besked</th><th>Resultat</th></tr></thead><tbody>{% for item in messages %}<tr><td>{{ item.received_at.strftime('%d/%m %H:%M') }}</td><td>{{ item.sender }}</td><td class="bodycell">{{ item.body }}</td><td><span class="tag">{{ 'Godkendt' if item.accepted else 'Afvist' }}</span></td></tr>{% else %}<tr><td colspan="4" class="empty">Ingen SMS'er registreret.</td></tr>{% endfor %}</tbody></table></div></section>
    <section class="card span12"><h2>Seneste WhatsApp-leveringer</h2><div class="tablewrap"><table><thead><tr><th>Tid</th><th>Modtager</th><th>Status</th><th>Fejl</th></tr></thead><tbody>{% for item in deliveries %}<tr><td>{{ item.attempted_at.strftime('%d/%m %H:%M') }}</td><td>{{ item.recipient_name }} · {{ item.recipient_phone }}</td><td><span class="tag">{{ item.status }}</span></td><td class="bodycell muted">{{ item.error or '' }}</td></tr>{% else %}<tr><td colspan="4" class="empty">Ingen WhatsApp-leveringer endnu.</td></tr>{% endfor %}</tbody></table></div></section>
  </div><div class="footer">SMS'er fra numre, der ikke står som aktive afsendere, bliver logget men ikke videresendt.</div>
</div>
""",
)


@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        db_state = "online"
    except Exception:  # noqa: BLE001
        db.session.rollback()
        db_state = "offline"
    wa = openwa_status()
    return jsonify(status="ok" if db_state == "online" else "degraded", database=db_state, openwa=wa)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username, admin_username()) and password_matches(password):
            session.clear()
            session["admin_authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            next_path = request.args.get("next", "")
            return redirect(next_path if next_path.startswith("/") and not next_path.startswith("//") else url_for("dashboard"))
        flash("Forkert brugernavn eller adgangskode.", "error")
    return render_template_string(LOGIN_HTML, title="Login")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    senders = AllowedSender.query.order_by(AllowedSender.name).all()
    recipients = Recipient.query.order_by(Recipient.name).all()
    messages = InboundMessage.query.order_by(InboundMessage.received_at.desc()).limit(50).all()
    deliveries = WhatsAppDelivery.query.order_by(WhatsAppDelivery.attempted_at.desc()).limit(50).all()
    return render_template_string(
        DASHBOARD_HTML,
        title="Administration",
        senders=senders,
        recipients=recipients,
        messages=messages,
        deliveries=deliveries,
        sender_count=sum(1 for item in senders if item.active),
        recipient_count=sum(1 for item in recipients if item.active),
        openwa=openwa_status(),
    )


@app.post("/senders")
@login_required
def create_sender():
    check_csrf()
    try:
        phone = normalize_phone(request.form.get("phone", ""))
        name = request.form.get("name", "").strip()
        if not name:
            raise ValueError("Navn mangler")
        if AllowedSender.query.filter_by(phone=phone).first():
            raise ValueError("Nummeret findes allerede")
        db.session.add(AllowedSender(name=name, phone=phone, active=True))
        db.session.commit()
        flash(f"{name} blev tilføjet som godkendt afsender.")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/senders/<int:sender_id>/toggle")
@login_required
def toggle_sender(sender_id: int):
    check_csrf()
    item = db.get_or_404(AllowedSender, sender_id)
    item.active = not item.active
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.post("/senders/<int:sender_id>/delete")
@login_required
def delete_sender(sender_id: int):
    check_csrf()
    item = db.get_or_404(AllowedSender, sender_id)
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.post("/recipients")
@login_required
def create_recipient():
    check_csrf()
    try:
        phone = normalize_phone(request.form.get("phone", ""))
        name = request.form.get("name", "").strip()
        if not name:
            raise ValueError("Navn mangler")
        if Recipient.query.filter_by(phone=phone).first():
            raise ValueError("Nummeret findes allerede")
        db.session.add(Recipient(name=name, phone=phone, active=True))
        db.session.commit()
        flash(f"{name} blev tilføjet som WhatsApp-modtager.")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/recipients/<int:recipient_id>/toggle")
@login_required
def toggle_recipient(recipient_id: int):
    check_csrf()
    item = db.get_or_404(Recipient, recipient_id)
    item.active = not item.active
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.post("/recipients/<int:recipient_id>/delete")
@login_required
def delete_recipient(recipient_id: int):
    check_csrf()
    item = db.get_or_404(Recipient, recipient_id)
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.post("/test")
@login_required
def test_message():
    check_csrf()
    recipients = Recipient.query.filter_by(active=True).order_by(Recipient.name).all()
    if not recipients:
        flash("Der er ingen aktive WhatsApp-modtagere.", "error")
        return redirect(url_for("dashboard"))
    sent = 0
    failed = 0
    for recipient in recipients:
        delivery = WhatsAppDelivery(
            inbound_id=None,
            recipient_name=recipient.name,
            recipient_phone=recipient.phone,
            status="pending",
            attempted_at=utcnow(),
        )
        db.session.add(delivery)
        db.session.commit()
        try:
            delivery.message_id = send_whatsapp(
                recipient.phone,
                "✅ Testbesked fra Racher SMS → WhatsApp gateway",
            )
            delivery.status = "sent"
            sent += 1
        except Exception as exc:  # noqa: BLE001
            delivery.status = "failed"
            delivery.error = str(exc)[:1000]
            failed += 1
        db.session.commit()
    flash(f"Test afsluttet: {sent} sendt, {failed} fejlet.", "error" if failed else "message")
    return redirect(url_for("dashboard"))


@app.post("/api/incoming")
def incoming():
    require_ingest_token()
    payload = request.get_json(force=True, silent=False) or {}
    try:
        sender = normalize_phone(payload.get("sender", ""))
        body = str(payload.get("body", "")).strip()
        if not body:
            raise ValueError("SMS-teksten er tom")
        received_at = parse_received_at(payload.get("receivedAt"))
        source_id = make_source_id(sender, body, received_at, payload.get("sourceMessageId"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    existing = InboundMessage.query.filter_by(source_id=source_id).first()
    if existing:
        return jsonify(id=existing.id, duplicate=True, accepted=existing.accepted), 200

    allowed = AllowedSender.query.filter_by(phone=sender, active=True).first() is not None
    inbound = InboundMessage(
        source_id=source_id,
        sender=sender,
        body=body,
        received_at=received_at,
        accepted=allowed,
    )
    db.session.add(inbound)
    db.session.commit()

    if not allowed:
        return jsonify(id=inbound.id, accepted=False, sent=0, failed=0), 202

    sent, failed = deliver_inbound(inbound)
    return jsonify(id=inbound.id, accepted=True, sent=sent, failed=failed), 201


with app.app_context():
    db.create_all()
