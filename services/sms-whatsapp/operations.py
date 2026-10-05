"""Operational controls. All sending remains in the existing serialized worker."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError
from flask import abort, flash, jsonify, redirect, render_template, request, has_request_context, send_file, url_for
import delivery_retry_app as delivery
import station_filter_app as stations
import admin_station_app as users
import history_app as history
import diagnostics

app, base, db = delivery.app, delivery.base, delivery.db
log = logging.getLogger("sbr-pager-operations")
VERSION = "2026.10-operations.3"
BOOTED_AT = base.utcnow()
try:
    BUILD_AT = base.parse_received_at(json.loads(Path(__file__).with_name("build_info.json").read_text())["built_at"])
except (OSError, ValueError, KeyError, TypeError):
    BUILD_AT = None
_backup_lock = threading.Lock()
_housekeeping_at = 0.0
_backup_retry_at = 0.0
_housekeeping_error = None

class AuditEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=base.utcnow, index=True)
    actor = db.Column(db.String(120), nullable=False)
    action = db.Column(db.String(120), nullable=False)
    changes = db.Column(db.Text, nullable=False)

class RoutingReview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id", ondelete="CASCADE"), unique=True, nullable=False)
    station = db.Column(db.String(20))
    event_key = db.Column(db.String(128), index=True)
    targets = db.Column(db.Text, nullable=False)

class DuplicateHint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id", ondelete="CASCADE"), unique=True, nullable=False)
    previous_id = db.Column(db.Integer, db.ForeignKey("inbound_message.id", ondelete="SET NULL"))

class QueueApproval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.Integer, db.ForeignKey("whats_app_delivery.id", ondelete="CASCADE"), unique=True, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=False, default=base.utcnow)


def setting(key, default=""):
    row = stations.PagerRuntimeSetting.query.filter_by(key=key).first()
    return row.value if row else default


def set_setting(key, value):
    row = stations.PagerRuntimeSetting.query.filter_by(key=key).first()
    if not row:
        row = stations.PagerRuntimeSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)


def number_setting(key, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(setting(key, str(default)))))
    except (TypeError, ValueError):
        return default


def runtime_mode():
    mode = setting("operation_mode", "normal")
    if mode not in {"maintenance", "pilot"}:
        return {"name": "normal", "until": None, "reason": ""}
    try:
        raw_until = setting("operation_until")
        if not raw_until:
            raise ValueError("missing expiry")
        until = base.parse_received_at(raw_until)
    except ValueError:
        return {"name": "maintenance", "until": None, "reason": "Ugyldig udløbstid; vælg Normal drift"}
    if until <= base.utcnow():
        return {"name": "normal", "until": None, "reason": ""}
    return {"name": mode, "until": until, "reason": setting("operation_reason")}


def sending_paused():
    return runtime_mode()["name"] != "normal"


def outgoing_body(inbound):
    if stations.station_for_inbound(inbound) == stations.TEST_STATION and not inbound.body.startswith("🧪 TEST"):
        return "🧪 TEST · ØVELSE\n" + inbound.body
    return inbound.body


def loop_reason(body):
    # Exact program-specific fingerprints only, never generic 'status'/'alarm'.
    text = body.strip()
    own_status = re.match(r"^(?:PI|MINI) (?:OK|FEJL) - temp \S+ - disk \S+% - RAM \S+% - load \S+ - Docker \S+ - up ", text)
    if own_status or text.startswith(("✅ SBR Pager · forbindelsestest #", "✅ TEST · Testbesked fra SBR Pager", "[SBR-SYSTEM]")):
        return "Systemets egen test/statusbesked: stoppet for at undgå en beskedsløjfe"
    return None


def selected_recipients(station):
    selections = stations.subscription_map()
    return [r for r in base.Recipient.query.filter_by(active=True).order_by(base.Recipient.name).all()
            if stations.accepts_selection(selections.get(r.id, {stations.ALL_STATIONS}), station)]


def preview_route(body, sender):
    station = stations.detect_station(body)
    quality, why = delivery.message_quality(body)
    allowed = stations.sms_sender_allowed(sender)
    loop = loop_reason(body)
    targets = selected_recipients(station)
    reason = "Afsendernummeret er ikke godkendt" if not allowed else loop or (why if delivery.QUALITY_FILTER_ENABLED and not quality else None)
    return {"station": station, "targets": targets, "reason": reason, "eligible": not reason}


def before_enqueue(inbound):
    why = loop_reason(inbound.body)
    if why:
        delivery._record_decision(inbound, "loop_blocked", why)
        inbound.accepted = False
        db.session.commit()
        return True
    cutoff = base.utcnow() - timedelta(minutes=5)
    previous = base.InboundMessage.query.filter(base.InboundMessage.id != inbound.id,
        base.InboundMessage.sender == inbound.sender, base.InboundMessage.body == inbound.body,
        base.InboundMessage.created_at >= cutoff).order_by(base.InboundMessage.id.desc()).first()
    if previous and not DuplicateHint.query.filter_by(inbound_id=inbound.id).first():
        db.session.add(DuplicateHint(inbound_id=inbound.id, previous_id=previous.id))
    if runtime_mode()["name"] == "pilot":
        payload = request.get_json(silent=True) if has_request_context() else {}
        payload = payload if isinstance(payload, dict) else {}
        station = stations.detect_station(inbound.body)
        parent_key = str(payload.get("parentEventKey") or "")[:128]
        if not station and parent_key:
            parent = RoutingReview.query.join(base.InboundMessage).filter(RoutingReview.event_key == parent_key, base.InboundMessage.sender == inbound.sender).order_by(RoutingReview.id.desc()).first()
            if parent:
                station = parent.station
        if not station:
            station = stations.station_for_inbound(inbound)
        targets = [{"name": r.name, "phone": r.phone} for r in selected_recipients(station)]
        db.session.add(RoutingReview(inbound_id=inbound.id, station=station, event_key=str(payload.get("eventKey") or payload.get("groupKey") or inbound.source_id)[:128], targets=json.dumps(targets, ensure_ascii=False)))
        delivery._record_decision(inbound, "pilot", "Prøvetilstand: modtaget og vurderet uden udsendelse. Genafsendes ikke efter skift til normal drift.")
        db.session.commit()
        return True
    return False


def hold_stale(state, row, now):
    approval = QueueApproval.query.filter_by(delivery_id=row.id).first()
    start = delivery._aware(approval.approved_at if approval else state.first_failed_at)
    if start and now - start >= timedelta(minutes=number_setting("stale_minutes", 15, 5, 1440)):
        row.status = "held"
        row.error = "Gammel alarm i køen: kræver godkendelse før afsendelse"
        state.next_attempt_at = None
        db.session.commit()
        return True
    return False


def recover_interrupted_alarms():
    rows = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.inbound_id.is_not(None), base.WhatsAppDelivery.status == "sending").all()
    for row in rows:
        row.status = "held"
        row.error = "Genstart under afsendelse: resultatet er uafklaret. Kontrollér WhatsApp før godkendelse."
        state = delivery._state_for(row.id)
        if not state:
            state = delivery.WhatsAppRetryState(delivery_id=row.id, attempts=0, first_failed_at=row.attempted_at or base.utcnow(), updated_at=base.utcnow())
            db.session.add(state)
        state.next_attempt_at, state.completed_at = None, None
    if rows:
        db.session.commit()
        record_audit("Genstart under afsendelse", {"tilbageholdte_jobs": len(rows)}, actor="System")
    return len(rows)


CONFIG_MODELS = {"recipients": base.Recipient, "senders": base.AllowedSender,
    "stations": users.AdminStation, "memberships": users.RecipientAdminStation,
    "filters": stations.RecipientStationFilter, "settings": stations.PagerRuntimeSetting}
CONFIG_FIELDS = {"recipients": ("id", "name", "phone", "active"), "senders": ("id", "name", "phone", "active"),
    "stations": ("id", "name"), "memberships": ("id", "recipient_id", "station_id"),
    "filters": ("id", "recipient_id", "station"), "settings": ("id", "key", "value")}
SAFE_SETTINGS = {stations.ACCEPT_ALL_SENDERS_KEY, stations.PREALERT_DELAY_KEY, "stale_minutes", "retention_days", "traffic_threshold"}


def config_snapshot():
    return {key: [{field: getattr(row, field) for field in CONFIG_FIELDS[key]} for row in model.query.order_by(model.id).all()]
            for key, model in CONFIG_MODELS.items()}


def record_audit(action, changes, actor=None):
    db.session.add(AuditEntry(actor=actor or getattr(base, 'audit_actor', base.admin_username)(), action=action[:120], changes=json.dumps(changes, ensure_ascii=False)))
    db.session.commit()


# Only configuration forms; no message bodies, login forms or credentials.
AUDIT_ENDPOINTS = {"create_admin_user", "update_admin_user", "delete_admin_user", "create_admin_station", "set_recipient_admin_station",
    "update_admin_user_alarm_stations", "update_sender_filter", "update_prealert_delay", "create_sender", "toggle_sender", "delete_sender",
    "create_recipient", "toggle_recipient", "delete_recipient", "update_recipient_stations", "operation_controls"}

def audited(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not base.logged_in():
            return fn(*args, **kwargs)
        with delivery._delivery_lock:
            before = config_snapshot()
            response = app.make_response(fn(*args, **kwargs))
            if response.status_code < 400:
                after = config_snapshot()
                changes = {key: {"før": before[key], "efter": after[key]} for key in before if before[key] != after[key]}
                if changes:
                    record_audit(fn.__name__, changes)
            return response
    return wrapped


def backup_directory():
    override = os.getenv("SMS_WHATSAPP_BACKUP_DIR")
    path = Path(override) if override else Path(db.engine.url.database).resolve().parent / "backups"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def backup_database():
    if db.engine.dialect.name != "sqlite" or db.engine.url.database in {None, ":memory:"}:
        raise ValueError("Automatisk backup kræver en SQLite-fil")
    with _backup_lock:
        directory = backup_directory()
        name = "pager-" + base.utcnow().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        target = directory / (name + ".sqlite")
        temporary = target.with_suffix(".tmp")
        try:
            with sqlite3.connect(db.engine.url.database, timeout=30) as source, sqlite3.connect(temporary) as destination:
                deadline = time.monotonic() + 30
                def progress(_status, _remaining, _total):
                    if time.monotonic() > deadline:
                        raise TimeoutError("Backup oversteg 30 sekunder")
                source.backup(destination, pages=256, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backupens integritetskontrol fejlede")
                # Export from this same consistent snapshot, not live ORM rows.
                config = {key: [dict(zip(CONFIG_FIELDS[key], values)) for values in destination.execute(
                    'SELECT ' + ','.join('"' + field + '"' for field in CONFIG_FIELDS[key]) + ' FROM "' + model.__tablename__ + '" ORDER BY id')]
                    for key, model in CONFIG_MODELS.items()}
            for key in ("recipients", "senders"):
                for row in config[key]:
                    row["active"] = bool(row["active"])
            os.chmod(temporary, 0o600)
            temporary.replace(target)
            manifest = {"format": "sbr-pager-config-v1", "version": VERSION, "created_at": base.utcnow().isoformat(), "config": config}
            config_path = directory / (name + ".json")
            config_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(config_path, 0o600)
            # Remove only complete backups generated here, retaining 14 pairs.
            for old in sorted(directory.glob("pager-*.sqlite"), reverse=True)[14:]:
                old.unlink()
                old.with_suffix(".json").unlink(missing_ok=True)
            return name
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            (directory / (name + ".json")).unlink(missing_ok=True)
            raise


def backup_list():
    return [{"name": f.stem, "size": f.stat().st_size, "at": f.stat().st_mtime} for f in sorted(backup_directory().glob("pager-*.sqlite"), reverse=True)[:14]]


def backup_path(name, suffix):
    if not re.fullmatch(r"pager-\d{8}T\d{6}-[0-9a-f]{6}", name):
        abort(404)
    path = backup_directory() / (name + suffix)
    if not path.is_file() or path.is_symlink():
        abort(404)
    return path


def validated_config(document):
    if not isinstance(document, dict) or document.get("format") != "sbr-pager-config-v1":
        raise ValueError("Ukendt backupformat")
    data = document.get("config")
    if not isinstance(data, dict) or set(data) != set(CONFIG_MODELS):
        raise ValueError("Backupen mangler konfigurationstabeller")
    for key, rows in data.items():
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("Ugyldig størrelse på konfiguration")
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(CONFIG_FIELDS[key]):
                raise ValueError("Ugyldige felter i konfiguration")
            if type(row["id"]) is not int or row["id"] <= 0 or row["id"] in ids:
                raise ValueError("Ugyldige eller gentagne id'er")
            ids.add(row["id"])
    for key in ("recipients", "senders"):
        phones = set()
        for row in data[key]:
            if not isinstance(row["name"], str) or not 1 <= len(row["name"].strip()) <= 120 or type(row["active"]) is not bool:
                raise ValueError("Ugyldigt navn eller aktiv-status")
            phone = base.normalize_phone(row["phone"])
            if phone in phones:
                raise ValueError("Gentaget telefonnummer")
            phones.add(phone)
            row["phone"] = phone
    station_ids = {r["id"] for r in data["stations"]}
    recipient_ids = {r["id"] for r in data["recipients"]}
    station_names = set()
    for row in data["stations"]:
        name = row["name"]
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 120 or name.casefold() in station_names:
            raise ValueError("Ugyldigt eller gentaget stationsnavn")
        station_names.add(name.casefold())
    seen_members, seen_filters, seen_keys = set(), set(), set()
    for row in data["memberships"]:
        if type(row["recipient_id"]) is not int or type(row["station_id"]) is not int:
            raise ValueError("Ugyldigt stationsmedlemskab")
        if row["recipient_id"] not in recipient_ids or row["station_id"] not in station_ids or row["recipient_id"] in seen_members:
            raise ValueError("Ugyldigt stationsmedlemskab")
        seen_members.add(row["recipient_id"])
    for row in data["filters"]:
        if type(row["recipient_id"]) is not int or not isinstance(row["station"], str):
            raise ValueError("Ugyldigt stationsfilter")
        pair = (row["recipient_id"], row["station"])
        if row["recipient_id"] not in recipient_ids or row["station"] not in set(stations.STATIONS) | {"*"} or pair in seen_filters:
            raise ValueError("Ugyldigt stationsfilter")
        seen_filters.add(pair)
    for row in data["settings"]:
        if not isinstance(row["key"], str) or len(row["key"]) > 64 or row["key"] in seen_keys or not isinstance(row["value"], str) or len(row["value"]) > 255:
            raise ValueError("Ugyldig indstilling")
        seen_keys.add(row["key"])
    return data


@delivery.serialized
def restore_configuration(name):
    document = json.loads(backup_path(name, ".json").read_text(encoding="utf-8"))
    data = validated_config(document)
    with base.ingest_lock:
        safety = backup_database()
        # Configuration only. Retain alarm history and never revive old jobs.
        for test in diagnostics.SingleWhatsAppTest.query.filter_by(status="pending").all():
            test.status, test.error, test.completed_at = "cancelled", "Konfigurationen blev gendannet", base.utcnow()
            row = db.session.get(base.WhatsAppDelivery, test.delivery_id)
            if row:
                row.status, row.error = "cancelled", test.error
        for row in base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.status.in_(("pending", "retrying", "held")), base.WhatsAppDelivery.inbound_id.is_not(None)).all():
            row.status, row.error = "cancelled", "Konfigurationen blev gendannet; levering genafsendes ikke"
            state = delivery._state_for(row.id)
            if state:
                state.completed_at, state.next_attempt_at = base.utcnow(), None
        db.session.flush()
        diagnostics.SingleWhatsAppTest.query.update({"recipient_id": None})
        for key in ("memberships", "filters", "recipients", "senders", "stations", "settings"):
            CONFIG_MODELS[key].query.delete(synchronize_session="fetch")
        db.session.flush()
        for key in ("stations", "recipients", "senders", "memberships", "filters", "settings"):
            for fields in data[key]:
                if key == "settings" and fields["key"] not in SAFE_SETTINGS:
                    continue
                db.session.add(CONFIG_MODELS[key](**fields))
            db.session.flush()
        set_setting("operation_mode", "maintenance")
        set_setting("operation_until", (base.utcnow() + timedelta(minutes=30)).isoformat())
        set_setting("operation_reason", "Kontrollér den gendannede opsætning før normal drift")
        db.session.commit()
        record_audit("Gendannelse af konfiguration", {"backup": name, "sikkerhedsbackup": safety})
        return safety


def prune_history(days):
    """Bounded batches; delete complete events only, preserving active jobs."""
    cutoff = base.utcnow() - timedelta(days=days)
    active = db.session.query(base.WhatsAppDelivery.inbound_id).filter(base.WhatsAppDelivery.status.in_(("pending", "retrying", "held")), base.WhatsAppDelivery.inbound_id.is_not(None))
    events = history.events.AlarmEvent.query.filter(history.events.AlarmEvent.last_update_at < cutoff).order_by(history.events.AlarmEvent.id).limit(100).all()
    removed = 0
    for event in events:
        links = history.events.AlarmEventMessage.query.filter_by(event_id=event.id).all()
        ids = [row.inbound_id for row in links]
        if ids and base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.inbound_id.in_(ids), base.WhatsAppDelivery.status.in_(("pending", "retrying", "held"))).first():
            continue
        history.AlarmEventLocation.query.filter_by(event_id=event.id).delete(synchronize_session=False)
        if ids:
            base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.inbound_id.in_(ids)).delete(synchronize_session=False)
            history.events.AlarmEventMessage.query.filter_by(event_id=event.id).delete(synchronize_session=False)
            base.InboundMessage.query.filter(base.InboundMessage.id.in_(ids)).delete(synchronize_session=False)
        db.session.delete(event)
        removed += 1
    standalone = base.InboundMessage.query.filter(base.InboundMessage.created_at < cutoff,
        ~base.InboundMessage.id.in_(active),
        ~base.InboundMessage.id.in_(db.session.query(history.events.AlarmEventMessage.inbound_id))).limit(100).all()
    for row in standalone:
        base.WhatsAppDelivery.query.filter_by(inbound_id=row.id).delete(synchronize_session=False)
        db.session.delete(row)
        removed += 1
    terminal_tests = diagnostics.SingleWhatsAppTest.query.filter(diagnostics.SingleWhatsAppTest.created_at < cutoff,
        diagnostics.SingleWhatsAppTest.status.notin_(("pending", "running"))).order_by(diagnostics.SingleWhatsAppTest.id).limit(100).all()
    for test in terminal_tests:
        row = db.session.get(base.WhatsAppDelivery, test.delivery_id) if test.delivery_id else None
        db.session.delete(test)
        if row:
            db.session.delete(row)
    legacy_logs = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.inbound_id.is_(None),
        base.WhatsAppDelivery.attempted_at < cutoff, base.WhatsAppDelivery.status.notin_(("pending", "sending")),
        ~base.WhatsAppDelivery.id.in_(db.session.query(diagnostics.SingleWhatsAppTest.delivery_id).filter(diagnostics.SingleWhatsAppTest.delivery_id.is_not(None)))).limit(100).all()
    for row in legacy_logs:
        db.session.delete(row)
    AuditEntry.query.filter(AuditEntry.at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return removed


@delivery.serialized
def housekeeping():
    global _housekeeping_at, _backup_retry_at, _housekeeping_error
    now = time.monotonic()
    if _housekeeping_at and now - _housekeeping_at < 60:
        return
    _housekeeping_at = now
    try:
        today = base.utcnow().astimezone(ZoneInfo("Europe/Copenhagen")).date().isoformat()
        if setting("last_daily_backup") != today and now >= _backup_retry_at:
            backup_database()
            set_setting("last_daily_backup", today)
            db.session.commit()
        days = number_setting("retention_days", 0, 0, 3650)
        if days and setting("last_daily_backup") == today:
            with base.ingest_lock:
                prune_history(days)
        if setting("last_daily_backup") == today:
            _housekeeping_error = None
    except Exception:
        db.session.rollback()
        _backup_retry_at = now + 3600
        _housekeeping_error = "Backup eller oprydning fejlede. Kontrollér diskplads og rettigheder."
        log.exception("Driftsvedligeholdelse fejlede")


def operation_snapshot():
    last_sms = base.InboundMessage.query.order_by(base.InboundMessage.created_at.desc()).first()
    last_sent = base.WhatsAppDelivery.query.filter_by(status="sent").order_by(base.WhatsAppDelivery.attempted_at.desc()).first()
    cutoff = base.utcnow() - timedelta(minutes=5)
    count = base.InboundMessage.query.filter(base.InboundMessage.created_at >= cutoff).count()
    failure_count = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.attempted_at >= cutoff, base.WhatsAppDelivery.status.in_(("failed", "retrying"))).count()
    return {"mode": runtime_mode(), "last_sms": last_sms.created_at if last_sms else None,
        "last_sent": last_sent.attempted_at if last_sent else None,
        "held": base.WhatsAppDelivery.query.filter_by(status="held").count(),
        "traffic_count": count, "traffic_warning": count >= number_setting("traffic_threshold", 20, 5, 1000),
        "failure_warning": failure_count >= 5, "housekeeping_error": _housekeeping_error,
        "last_backup": setting("last_daily_backup", "Ingen automatisk backup endnu"), "version": VERSION}


@app.context_processor
def operational_context():
    return {"pager_version": VERSION, "pager_built_at": BUILD_AT, "operation_mode": runtime_mode() if base.logged_in() else {"name": "normal"}}

@app.get("/drift")
@base.login_required
def operations_page():
    held = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.status.in_(("held", "failed")), base.WhatsAppDelivery.inbound_id.is_not(None)).order_by(base.WhatsAppDelivery.id).limit(100).all()
    return render_template("operations.html", title="Drift & backup", ops=operation_snapshot(), backups=backup_list(), held=held,
        stale=number_setting("stale_minutes",15,5,1440), retention=number_setting("retention_days",0,0,3650), threshold=number_setting("traffic_threshold",20,5,1000))

@app.post("/drift/indstillinger")
@base.login_required
@delivery.serialized
def operation_controls():
    base.check_csrf()
    mode = request.form.get("mode")
    if mode not in {"normal", "pilot", "maintenance"}:
        abort(400)
    try:
        minutes = int(request.form.get("minutes", "30"))
        stale = int(request.form.get("stale", "15"))
        retention = int(request.form.get("retention", "0"))
        threshold = int(request.form.get("threshold", "20"))
    except ValueError:
        abort(400)
    if not (5 <= minutes <= 120 and 5 <= stale <= 1440 and (retention == 0 or 30 <= retention <= 3650) and 5 <= threshold <= 1000):
        abort(400, "Indstillingerne er uden for det tilladte interval")
    with base.ingest_lock:
        set_setting("operation_mode", mode)
        set_setting("operation_until", (base.utcnow() + timedelta(minutes=minutes)).isoformat())
        set_setting("operation_reason", request.form.get("reason", "").strip()[:160])
        for key, value in (("stale_minutes",stale),("retention_days",retention),("traffic_threshold",threshold)):
            set_setting(key,value)
        db.session.commit()
    delivery._wake_event.set()
    flash("Driftsindstillingerne er gemt.")
    return redirect(url_for("operations_page"))

@app.post("/drift/backup")
@base.login_required
@delivery.serialized
def manual_backup():
    base.check_csrf()
    try:
        name = backup_database()
        record_audit("Manuel backup", {"backup": name})
        flash("Backup oprettet og integritetskontrolleret.")
    except (OSError, sqlite3.Error, ValueError, TimeoutError):
        db.session.rollback()
        flash("Backup fejlede. Kontrollér diskplads og rettigheder.", "error")
    return redirect(url_for("operations_page"))

@app.get("/drift/backup/<name>/<kind>")
@base.login_required
def download_backup(name, kind):
    if kind not in {"database", "config"}:
        abort(404)
    return send_file(backup_path(name, ".sqlite" if kind == "database" else ".json"), as_attachment=True)

@app.post("/drift/backup/<name>/gendan")
@base.login_required
def restore_backup(name):
    base.check_csrf()
    if request.form.get("confirm") != "GENDAN":
        abort(400, "Skriv GENDAN for at erstatte konfigurationen")
    try:
        restore_configuration(name)
        flash("Opsætningen er gendannet. Historik er bevaret, gamle jobs annulleret og vedligeholdelse aktiveret i 30 minutter.")
    except (ValueError, OSError, sqlite3.Error, SQLAlchemyError, TimeoutError):
        db.session.rollback()
        flash("Gendannelse fejlede; konfigurationen blev ikke ændret.", "error")
    return redirect(url_for("operations_page"))

@app.post("/drift/ko/<int:inbound_id>")
@base.login_required
@delivery.serialized
def review_old_alarm(inbound_id):
    base.check_csrf()
    action = request.form.get("action")
    if action not in {"release", "cancel"} or request.form.get("confirm") != "1":
        abort(400)
    if action == "release" and sending_paused():
        abort(409, "Vælg normal drift før afsendelse")
    rows = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.inbound_id == inbound_id, base.WhatsAppDelivery.status.in_(("held", "failed") if action == "cancel" else ("held",))).all()
    for row in rows:
        state = delivery._state_for(row.id)
        if action == "cancel":
            row.status, row.error = "cancelled", "Gammel alarm fravalgt af administrator"
            if state:
                state.completed_at, state.next_attempt_at = base.utcnow(), None
        else:
            row.status, row.error = "pending", None
            if state:
                state.next_attempt_at, state.completed_at = base.utcnow(), None
                state.first_failed_at, state.attempts = base.utcnow(), 0
            approval = QueueApproval.query.filter_by(delivery_id=row.id).first()
            if approval:
                approval.approved_at = base.utcnow()
            else:
                db.session.add(QueueApproval(delivery_id=row.id))
    db.session.commit()
    if rows:
        record_audit("Vurdering af gammel alarm", {"inbound_id": inbound_id, "action": action, "jobs": len(rows)})
    delivery._wake_event.set()
    return redirect(url_for("operations_page"))

@app.route("/forhaandsvisning", methods=["GET", "POST"])
@base.login_required
def routing_preview():
    result = None
    if request.method == "POST":
        base.check_csrf()
        try:
            sender = base.normalize_phone(request.form.get("sender", ""))
        except ValueError:
            abort(400, "Ugyldigt afsendernummer")
        body = request.form.get("body", "").strip()
        if not body or len(body) > 10000:
            abort(400, "Indtast højst 10.000 tegn")
        result = preview_route(body, sender)
    return render_template("preview.html", title="Forhåndsvis besked", result=result)

@app.get("/aendringer")
@base.login_required
def audit_page():
    rows = AuditEntry.query.order_by(AuditEntry.id.desc()).limit(100).all()
    return render_template("audit.html", title="Ændringshistorik", rows=rows)


def config_issues():
    issues = []
    active = base.Recipient.query.filter_by(active=True).all()
    phones = set()
    for row in active:
        try:
            phone = base.normalize_phone(row.phone)
            if phone in phones:
                issues.append("Dubleret modtagernummer: " + row.name)
            phones.add(phone)
        except ValueError:
            issues.append("Ugyldigt modtagernummer: " + row.name)
    selections = stations.subscription_map()
    for station in stations.STATIONS:
        if not any(stations.accepts_selection(selections.get(row.id, {stations.ALL_STATIONS}), station) for row in active):
            issues.append("Ingen aktive modtagere for " + ("Test" if station == "TEST" else station))
    return issues

@app.get("/opstartskontrol")
@base.login_required
def startup_check():
    import dashboard_app
    data = dashboard_app.snapshot()
    try:
        if db.engine.dialect.name == "sqlite":
            with db.engine.connect() as conn:
                db_ok = conn.exec_driver_sql("PRAGMA quick_check").scalar() == "ok"
        else:
            db.session.execute(db.text("SELECT 1"))
            db_ok = True
    except Exception:
        db.session.rollback()
        db_ok = False
    return render_template("startup.html", title="Opstartskontrol", data=data, db_ok=db_ok, issues=config_issues(), boot=BOOTED_AT)

@app.get("/driftsvisning")
@base.login_required
def station_display():
    import dashboard_app
    data = dashboard_app.snapshot()
    return render_template("display.html", title="Driftsvisning", data=data, ops=operation_snapshot())

@app.get("/fejlrapport.json")
@base.login_required
def diagnostic_export():
    import dashboard_app
    data = dashboard_app.snapshot()
    # Explicit whitelist: no message text, numbers, URLs, tokens or raw errors.
    report = {"version": VERSION, "booted_at": BOOTED_AT, "generated_at": base.utcnow(), "mode": runtime_mode()["name"],
        "internet": data["internet"].get("state"), "sms": {"state": data["modem"].get("state"), "transport": data["modem"].get("transport"), "capability": data["modem"].get("capability")},
        "whatsapp": data["wa"].get("state"), "queue": data["queue"], "held": operation_snapshot()["held"],
        "decisions": dict(db.session.query(delivery.InboundDecision.decision, db.func.count(delivery.InboundDecision.id)).group_by(delivery.InboundDecision.decision).all()),
        "delivery_states": dict(db.session.query(base.WhatsAppDelivery.status, db.func.count(base.WhatsAppDelivery.id)).group_by(base.WhatsAppDelivery.status).all())}
    response = jsonify(report)
    response.headers["Content-Disposition"] = 'attachment; filename="sbr-pager-fejlrapport.json"'
    return response


@app.get("/beskeder")
@base.login_required
def message_history():
    query = base.InboundMessage.query
    text = request.args.get("q", "").strip()[:200]
    sender = request.args.get("sender", "").strip()[:30]
    status = request.args.get("status", "")
    station = request.args.get("station", "").upper()
    if text:
        query = query.filter(base.InboundMessage.body.contains(text, autoescape=True))
    if sender:
        query = query.filter(base.InboundMessage.sender.contains(sender, autoescape=True))
    if station in stations.STATIONS:
        ids = db.session.query(history.events.AlarmEventMessage.inbound_id).join(history.events.AlarmEvent).filter(history.events.AlarmEvent.station == station)
        if station == "TEST":
            ids = db.session.query(history.events.AlarmEventMessage.inbound_id).filter(history.events.AlarmEventMessage.kind == "test")
        pilot_ids = db.session.query(RoutingReview.inbound_id).filter_by(station=station)
        query = query.filter(db.or_(base.InboundMessage.id.in_(ids), base.InboundMessage.id.in_(pilot_ids)))
    if status == "rejected":
        query = query.filter_by(accepted=False)
    elif status in {"sent", "pending", "retrying", "failed", "held", "cancelled"}:
        ids = db.session.query(base.WhatsAppDelivery.inbound_id).filter_by(status=status)
        query = query.filter(base.InboundMessage.id.in_(ids))
    elif status == "pilot":
        query = query.filter(base.InboundMessage.id.in_(db.session.query(RoutingReview.inbound_id)))
    for key, op in (("from", ">="), ("to", "<")):
        value = request.args.get(key, "")
        if value:
            try:
                from datetime import datetime
                date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Europe/Copenhagen"))
                if key == "to":
                    date += timedelta(days=1)
            except ValueError:
                abort(400, "Ugyldig dato")
            date = date.astimezone(delivery.timezone.utc)
            query = query.filter(base.InboundMessage.received_at >= date if op == ">=" else base.InboundMessage.received_at < date)
    try:
        page = max(1, min(100000, int(request.args.get("page", "1"))))
    except ValueError:
        abort(400)
    rows = query.order_by(base.InboundMessage.id.desc()).offset((page-1)*50).limit(51).all()
    args = request.args.to_dict()
    args["page"] = page+1
    next_url = url_for("message_history", **args) if len(rows)>50 else None
    args["page"] = page-1
    prev_url = url_for("message_history", **args) if page>1 else None
    return render_template("messages.html", title="Beskedhistorik", rows=rows[:50], next_url=next_url, prev_url=prev_url, station_codes=stations.STATIONS)

@app.get("/beskeder/<int:inbound_id>")
@base.login_required
def message_detail(inbound_id):
    inbound = db.get_or_404(base.InboundMessage, inbound_id)
    rows = base.WhatsAppDelivery.query.filter_by(inbound_id=inbound.id).order_by(base.WhatsAppDelivery.id).all()
    review = RoutingReview.query.filter_by(inbound_id=inbound.id).first()
    hint = DuplicateHint.query.filter_by(inbound_id=inbound.id).first()
    decision = delivery.InboundDecision.query.filter_by(inbound_id=inbound.id).first()
    return render_template("message_detail.html", title=f"Besked #{inbound.id}", inbound=inbound, rows=rows,
        decision=decision, reason=diagnostics.explain_inbound(inbound), review=json.loads(review.targets) if review else None,
        duplicate=hint.previous_id if hint else None, retries={r.id: delivery._state_for(r.id) for r in rows})


with app.app_context():
    db.create_all()
    # create_all does not add new indexes to existing installations.
    if db.engine.dialect.name == "sqlite":
        for _name, _table, _columns in (("ix_pager_inbound_created","inbound_message","created_at"),
                ("ix_pager_inbound_received","inbound_message","received_at"),
                ("ix_pager_delivery_status_time","whats_app_delivery","status,attempted_at")):
            db.session.execute(db.text(f"CREATE INDEX IF NOT EXISTS {_name} ON {_table} ({_columns})"))
        db.session.commit()
delivery.operations_before_enqueue = before_enqueue
delivery.operations_paused = sending_paused
delivery.operations_hold_stale = hold_stale
delivery.operations_body = outgoing_body
delivery.operations_housekeeping = housekeeping

delivery.operations_recover = recover_interrupted_alarms


_original_record_alarm_event = history.events.record_alarm_event
def record_production_event(inbound, payload):
    # Pilot reception must not change real alarm statistics or active incidents.
    if RoutingReview.query.filter_by(inbound_id=inbound.id).first():
        return
    return _original_record_alarm_event(inbound, payload)
history.events.record_alarm_event = record_production_event
