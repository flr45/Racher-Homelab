import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import serial
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from gsm0338 import decode_modem_bytes, encode_gsm0338

STATION_CODES = {
    "A": "Slagelse",
    "B": "Storebælt",
    "S": "Sorø",
    "K": "Korsør",
    "L": "Skælskør",
    "R": "Ruds Vedby",
    "ISL": "ISL",
}
PARENTHESIZED_START_PATTERN = re.compile(r"\((ISL|[ABSKLR])\)", re.IGNORECASE)
STANDALONE_ISL_PATTERN = re.compile(r"(^|[^A-Z0-9])ISL(?=$|[^A-Z0-9])", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{6,14}$")
MODEM_STATUS_FILE = Path(os.getenv("MODEM_STATUS_FILE", "/data/modem-status.json"))
GATEWAY_STATUS_FILE = Path(os.getenv("GATEWAY_STATUS_FILE", "/data/gateway-status.json"))

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
    code = db.Column(db.String(3), unique=True, nullable=False)
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
    station_code = db.Column(db.String(3), nullable=False)
    opened_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), index=True, nullable=False)


class InboundMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(20), nullable=False)
    body = db.Column(db.Text, nullable=False)
    station_code = db.Column(db.String(3))
    received_at = db.Column(db.DateTime(timezone=True), nullable=False)


class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id"), nullable=False)
    recipient = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    error = db.Column(db.Text)
    attempted_at = db.Column(db.DateTime(timezone=True), nullable=False)


modem_lock = threading.Lock()
status_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc)


def utc_iso():
    return utcnow().isoformat()


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s()-]", "", value or "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not PHONE_PATTERN.fullmatch(phone):
        raise ValueError("Ugyldigt telefonnummer")
    return phone


def parse_received_at(value):
    if not value:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Ugyldigt modtagelsestidspunkt") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_status_file(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_gateway_status(**values):
    with status_lock:
        current = read_status_file(GATEWAY_STATUS_FILE)
        current.update(values, updated_at=utc_iso())
        GATEWAY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = GATEWAY_STATUS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        temporary.replace(GATEWAY_STATUS_FILE)


def modem_command(port, command, expected="OK", timeout=8):
    port.reset_input_buffer()
    port.write((command + "\r").encode("ascii"))
    deadline = time.monotonic() + timeout
    response = bytearray()
    while time.monotonic() < deadline:
        response.extend(port.read(port.in_waiting or 1))
        response_text = decode_modem_bytes(bytes(response))
        if expected in response_text or "ERROR" in response_text:
            return response_text
    raise TimeoutError(f"Modem svarede ikke på {command}")


def send_sms(recipient: str, body: str):
    if os.getenv("SMS_DRY_RUN", "false").lower() == "true":
        log.info("DRY RUN SMS til %s: %s", recipient, body)
        return

    device = os.getenv("MODEM_DEVICE", "/dev/ttyUSB0")
    baudrate = int(os.getenv("MODEM_BAUDRATE", "115200"))
    disable_dtr_toggle = os.getenv("MODEM_DISABLE_DTR_TOGGLE", "true").lower() == "true"
    with modem_lock, serial.Serial(
        device,
        baudrate=baudrate,
        timeout=0.25,
        dsrdtr=disable_dtr_toggle,
    ) as port:
        modem_command(port, "AT")
        modem_command(port, 'AT+CSCS="GSM"')
        modem_command(port, "AT+CMGF=1")
        modem_command(port, f'AT+CMGS="{recipient}"', expected=">")
        port.write(encode_gsm0338(body) + b"\x1a")
        deadline = time.monotonic() + 25
        response = bytearray()
        while time.monotonic() < deadline:
            response.extend(port.read(port.in_waiting or 1))
            response_text = decode_modem_bytes(bytes(response))
            if "+CMGS:" in response_text and "OK" in response_text:
                return
            if "ERROR" in response_text:
                raise RuntimeError(response_text.strip())
        raise TimeoutError("SMS-afsendelse fik ikke kvittering fra modem")


def detect_station_code(body: str):
    parenthesized = PARENTHESIZED_START_PATTERN.search(body)
    if parenthesized:
        return parenthesized.group(1).upper()
    if STANDALONE_ISL_PATTERN.search(body):
        return "ISL"
    return None


def resolve_station(sender: str, body: str):
    now = utcnow()
    code = detect_station_code(body)
    if code:
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
    if active:
        return active.station_code

    candidates = (
        ActiveAlarm.query.filter(ActiveAlarm.expires_at >= now)
        .order_by(ActiveAlarm.opened_at.desc())
        .limit(2)
        .all()
    )
    if len(candidates) == 1:
        candidate = candidates[0]
        db.session.add(
            ActiveAlarm(
                sender=sender,
                station_code=candidate.station_code,
                opened_at=candidate.opened_at,
                expires_at=candidate.expires_at,
            )
        )
        return candidate.station_code

    return None


def build_vagtbytte_payload(
    sender: str,
    body: str,
    received_at: datetime,
    source_message_id: str | None,
    station_code: str | None,
):
    return {
        "senderNumber": sender,
        "rawMessage": body,
        "receivedAt": received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sourceMessageId": source_message_id,
        "stationCode": station_code,
    }


def forward_to_vagtbytte(
    sender: str,
    body: str,
    received_at: datetime,
    source_message_id: str | None,
    station_code: str | None,
):
    url = os.getenv(
        "VAGTBYTTE_ALARM_FEED_URL",
        "http://vagtbytte-web:3000/api/alarm-feed/ingest",
    ).strip()
    token = os.getenv("VAGTBYTTE_ALARM_FEED_TOKEN", "").strip()

    if not url:
        raise RuntimeError("VAGTBYTTE_ALARM_FEED_URL mangler")
    if not token:
        raise RuntimeError("VAGTBYTTE_ALARM_FEED_TOKEN mangler")

    payload = json.dumps(
        build_vagtbytte_payload(
            sender,
            body,
            received_at,
            source_message_id,
            station_code,
        ),
        ensure_ascii=False,
    ).encode("utf-8")
    outgoing = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(outgoing, timeout=10) as response:
            response_body = response.read().decode("utf-8")
            if response.status not in {200, 201}:
                raise RuntimeError(f"Vagtbytte svarede HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Vagtbytte svarede HTTP {exc.code}: {details[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Kunne ikke kontakte Vagtbytte: {exc.reason}") from exc

    try:
        return json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Vagtbytte returnerede ugyldigt JSON") from exc


def process_incoming(
    sender: str,
    body: str,
    received_at=None,
    source_message_id: str | None = None,
):
    sender = normalize_phone(sender)
    body = (body or "").strip()
    if not body:
        raise ValueError("SMS-teksten er tom")

    received_at = parse_received_at(received_at)
    station_code = resolve_station(sender, body)
    inbound = InboundMessage(
        sender=sender,
        body=body,
        station_code=station_code,
        received_at=received_at,
    )
    db.session.add(inbound)
    write_gateway_status(
        state="processing",
        database="online",
        last_received_sms_at=received_at.isoformat(),
        last_error=None,
    )

    try:
        db.session.flush()
        vagtbytte_result = forward_to_vagtbytte(
            sender=sender,
            body=body,
            received_at=received_at,
            source_message_id=source_message_id or f"sms-gateway:{inbound.id}",
            station_code=station_code,
        )
        db.session.commit()
        write_gateway_status(
            state="online",
            database="online",
            last_received_sms_at=received_at.isoformat(),
            last_vagtbytte_success_at=utc_iso(),
            last_vagtbytte_error=None,
            last_vagtbytte_error_at=None,
            last_error=None,
        )
    except Exception as exc:
        db.session.rollback()
        write_gateway_status(
            state="degraded",
            database="online",
            last_received_sms_at=received_at.isoformat(),
            last_vagtbytte_error=str(exc)[:1000],
            last_vagtbytte_error_at=utc_iso(),
            last_error=str(exc)[:1000],
        )
        raise

    if not station_code:
        return inbound, 0, vagtbytte_result

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

    return inbound, len(unique), vagtbytte_result


@app.get("/health")
def health():
    database_status = "online"
    database_error = None
    latest_message_at = None

    try:
        db.session.execute(text("SELECT 1"))
        latest_message = InboundMessage.query.order_by(InboundMessage.received_at.desc()).first()
        if latest_message:
            latest_message_at = latest_message.received_at.isoformat()
    except Exception as exc:
        database_status = "offline"
        database_error = str(exc)[:1000]
        db.session.rollback()

    modem_status = read_status_file(MODEM_STATUS_FILE)
    gateway_status = read_status_file(GATEWAY_STATUS_FILE)
    gateway_last_received = gateway_status.get("last_received_sms_at") or latest_message_at
    modem_state = str(modem_status.get("state") or "unknown").lower()
    overall_status = (
        "ok"
        if database_status == "online" and modem_state == "online"
        else "degraded"
    )

    return jsonify(
        status=overall_status,
        checked_at=utc_iso(),
        modem={
            "state": modem_state,
            "device": modem_status.get("device") or os.getenv("MODEM_DEVICE", "/dev/ttyUSB0"),
            "updated_at": modem_status.get("updated_at"),
            "last_message_at": modem_status.get("last_message_at"),
            "last_error": modem_status.get("last_error"),
            "network": modem_status.get("network"),
            "signal": modem_status.get("signal"),
        },
        gateway={
            "state": gateway_status.get("state", "unknown"),
            "database": database_status,
            "database_error": database_error,
            "updated_at": gateway_status.get("updated_at"),
            "last_received_sms_at": gateway_last_received,
            "last_vagtbytte_success_at": gateway_status.get("last_vagtbytte_success_at"),
            "last_vagtbytte_error_at": gateway_status.get("last_vagtbytte_error_at"),
            "last_vagtbytte_error": gateway_status.get("last_vagtbytte_error"),
            "last_error": gateway_status.get("last_error"),
        },
    )


@app.post("/api/incoming")
def incoming():
    payload = request.get_json(force=True)
    try:
        inbound, recipients, vagtbytte_result = process_incoming(
            payload.get("sender", ""),
            payload.get("body", ""),
            payload.get("receivedAt"),
            payload.get("sourceMessageId"),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        log.exception("Videresendelse til Vagtbytte fejlede")
        return jsonify(error=str(exc)), 502
    return jsonify(
        id=inbound.id,
        station=inbound.station_code,
        forwarded_immediately_to=recipients,
        vagtbytte_created=bool(vagtbytte_result.get("created")),
        vagtbytte_alarm_id=vagtbytte_result.get("alarmId"),
        vagtbytte_sequence=vagtbytte_result.get("sequenceNumber"),
    ), 201


@app.get("/api/stations")
def stations():
    return jsonify([{"code": station.code, "name": station.name} for station in Station.query.order_by(Station.name)])


@app.get("/api/firefighters")
def firefighters():
    people = Firefighter.query.order_by(Firefighter.name).all()
    return jsonify([
        {
            "id": person.id,
            "name": person.name,
            "phone": person.phone,
            "active": person.active,
            "stations": [station.code for station in person.stations],
        }
        for person in people
    ])


@app.post("/api/firefighters")
def create_firefighter():
    payload = request.get_json(force=True)
    try:
        phone = normalize_phone(payload.get("phone", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    codes = {str(code).upper() for code in payload.get("stations", [])}
    selected_stations = Station.query.filter(Station.code.in_(codes)).all() if codes else []
    if codes != {station.code for station in selected_stations}:
        return jsonify(error="En eller flere stationskoder findes ikke"), 400
    person = Firefighter(
        name=(payload.get("name") or "").strip(),
        phone=phone,
        active=bool(payload.get("active", True)),
        stations=selected_stations,
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
        selected_stations = Station.query.filter(Station.code.in_(codes)).all() if codes else []
        if codes != {station.code for station in selected_stations}:
            return jsonify(error="En eller flere stationskoder findes ikke"), 400
        person.stations = selected_stations
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
    write_gateway_status(state="online", database="online", last_error=None)
