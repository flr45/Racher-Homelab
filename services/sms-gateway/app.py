import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import serial
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy

STATION_CODES = {
    "A": "Slagelse",
    "S": "Sorø",
    "K": "Korsør",
    "L": "Skælskør",
    "R": "Ruds Vedby",
}
START_PATTERN = re.compile(r"\(([ASKLR])\)", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{6,14}$")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:////data/sms-gateway.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("sms-gateway")


firefighter_stations = db.Table(
    "firefighter_stations",
    db.Column("firefighter_id", db.Integer, db.ForeignKey("firefighter.id"), primary_key=True),
    db.Column("station_id", db.Integer, db.ForeignKey("station.id"), primary_key=True),
)


class Station(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(1), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)


class Firefighter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    stations = db.relationship("Station", secondary=firefighter_stations, lazy="selectin")


class ActiveAlarm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(20), index=True, nullable=False)
    station_code = db.Column(db.String(1), nullable=False)
    opened_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), index=True, nullable=False)


class InboundMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(20), nullable=False)
    body = db.Column(db.Text, nullable=False)
    station_code = db.Column(db.String(1))
    received_at = db.Column(db.DateTime(timezone=True), nullable=False)


class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id"), nullable=False)
    recipient = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    error = db.Column(db.Text)
    attempted_at = db.Column(db.DateTime(timezone=True), nullable=False)


modem_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc)


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s()-]", "", value or "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not PHONE_PATTERN.fullmatch(phone):
        raise ValueError("Ugyldigt telefonnummer")
    return phone


def modem_command(port, command, expected="OK", timeout=8):
    port.reset_input_buffer()
    port.write((command + "\r").encode("ascii"))
    deadline = time.monotonic() + timeout
    response = b""
    while time.monotonic() < deadline:
        response += port.read(port.in_waiting or 1)
        text = response.decode(errors="replace")
        if expected in text or "ERROR" in text:
            return text
    raise TimeoutError(f"Modem svarede ikke på {command}")


def send_sms(recipient: str, body: str):
    if os.getenv("SMS_DRY_RUN", "false").lower() == "true":
        log.info("DRY RUN SMS til %s: %s", recipient, body)
        return

    device = os.getenv("MODEM_DEVICE", "/dev/ttyUSB0")
    baudrate = int(os.getenv("MODEM_BAUDRATE", "115200"))
    with modem_lock, serial.Serial(device, baudrate=baudrate, timeout=0.25) as port:
        modem_command(port, "AT")
        modem_command(port, "AT+CMGF=1")
        modem_command(port, f'AT+CMGS="{recipient}"', expected=">")
        port.write(body.encode("utf-8") + b"\x1a")
        deadline = time.monotonic() + 25
        response = b""
        while time.monotonic() < deadline:
            response += port.read(port.in_waiting or 1)
            text = response.decode(errors="replace")
            if "+CMGS:" in text and "OK" in text:
                return
            if "ERROR" in text:
                raise RuntimeError(text.strip())
        raise TimeoutError("SMS-afsendelse fik ikke kvittering fra modem")


def resolve_station(sender: str, body: str):
    now = utcnow()
    match = START_PATTERN.search(body)
    if match:
        code = match.group(1).upper()
        minutes = int(os.getenv("ALARM_SOURCE_WINDOW_MINUTES", "10"))
        ActiveAlarm.query.filter_by(sender=sender).delete()
        db.session.add(
            ActiveAlarm(
                sender=sender,
                station_code=code,
                opened_at=now,
                expires_at=now + timedelta(minutes=minutes),
            )
        )
        return code

    active = (
        ActiveAlarm.query.filter_by(sender=sender)
        .filter(ActiveAlarm.expires_at >= now)
        .order_by(ActiveAlarm.opened_at.desc())
        .first()
    )
    return active.station_code if active else None


def process_incoming(sender: str, body: str):
    sender = normalize_phone(sender)
    body = (body or "").strip()
    if not body:
        raise ValueError("SMS-teksten er tom")

    station_code = resolve_station(sender, body)
    inbound = InboundMessage(sender=sender, body=body, station_code=station_code, received_at=utcnow())
    db.session.add(inbound)
    db.session.commit()

    if not station_code:
        return inbound, 0

    recipients = (
        Firefighter.query.join(Firefighter.stations)
        .filter(Firefighter.active.is_(True), Station.code == station_code)
        .all()
    )
    unique = {person.phone for person in recipients if person.phone != sender}

    for recipient in unique:
        delivery = Delivery(
            inbound_id=inbound.id,
            recipient=recipient,
            status="pending",
            attempted_at=utcnow(),
        )
        db.session.add(delivery)
        db.session.commit()
        try:
            send_sms(recipient, body)
            delivery.status = "sent"
        except Exception as exc:
            log.exception("SMS til %s fejlede", recipient)
            delivery.status = "failed"
            delivery.error = str(exc)[:1000]
        db.session.commit()

    return inbound, len(unique)


@app.get("/health")
def health():
    return jsonify(status="ok", modem_device=os.getenv("MODEM_DEVICE", "/dev/ttyUSB0"))


@app.post("/api/incoming")
def incoming():
    payload = request.get_json(force=True)
    try:
        inbound, recipients = process_incoming(payload.get("sender", ""), payload.get("body", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(
        id=inbound.id,
        station=inbound.station_code,
        forwarded_immediately_to=recipients,
    ), 201


@app.get("/api/stations")
def stations():
    return jsonify([{"code": s.code, "name": s.name} for s in Station.query.order_by(Station.name)])


@app.get("/api/firefighters")
def firefighters():
    people = Firefighter.query.order_by(Firefighter.name).all()
    return jsonify([
        {
            "id": p.id,
            "name": p.name,
            "phone": p.phone,
            "active": p.active,
            "stations": [s.code for s in p.stations],
        }
        for p in people
    ])


@app.post("/api/firefighters")
def create_firefighter():
    payload = request.get_json(force=True)
    try:
        phone = normalize_phone(payload.get("phone", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    codes = {str(code).upper() for code in payload.get("stations", [])}
    stations = Station.query.filter(Station.code.in_(codes)).all() if codes else []
    if codes != {s.code for s in stations}:
        return jsonify(error="En eller flere stationskoder findes ikke"), 400
    person = Firefighter(
        name=(payload.get("name") or "").strip(),
        phone=phone,
        active=bool(payload.get("active", True)),
        stations=stations,
    )
    if not person.name:
        return jsonify(error="Navn mangler"), 400
    db.session.add(person)
    db.session.commit()
    return jsonify(id=person.id), 201


@app.put("/api/firefighters/<int:person_id>")
def update_firefighter(person_id):
    person = db.get_or_404(Firefighter, person_id)
    payload = request.get_json(force=True)
    if "name" in payload:
        person.name = str(payload["name"]).strip()
    if "phone" in payload:
        try:
            person.phone = normalize_phone(payload["phone"])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    if "active" in payload:
        person.active = bool(payload["active"])
    if "stations" in payload:
        codes = {str(code).upper() for code in payload["stations"]}
        stations = Station.query.filter(Station.code.in_(codes)).all() if codes else []
        if codes != {s.code for s in stations}:
            return jsonify(error="En eller flere stationskoder findes ikke"), 400
        person.stations = stations
    db.session.commit()
    return jsonify(status="updated")


@app.get("/api/messages")
def messages():
    rows = InboundMessage.query.order_by(InboundMessage.received_at.desc()).limit(100).all()
    return jsonify([
        {
            "id": row.id,
            "sender": row.sender,
            "body": row.body,
            "station": row.station_code,
            "received_at": row.received_at.isoformat(),
        }
        for row in rows
    ])


with app.app_context():
    db.create_all()
    for code, name in STATION_CODES.items():
        if not Station.query.filter_by(code=code).first():
            db.session.add(Station(code=code, name=name))
    db.session.commit()
