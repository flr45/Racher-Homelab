"""Independent, bounded monitoring. No alarm body or recipient leaves this module."""
import json
import logging
import os
from pathlib import Path
import re
import shutil
import smtplib
import ssl
import threading
import urllib.parse
import urllib.request
from datetime import timedelta
from email.message import EmailMessage

from flask import flash, redirect, render_template, request, url_for
import diagnostics
import delivery_retry_app as delivery
import operations as ops
from operator_accounts import admin_required

app, base, db = ops.app, ops.base, ops.db
log = logging.getLogger('pager-resilience')
_stop = threading.Event()
_started = False
_wake = threading.Event()
LABELS = {'internet': 'Internet', 'modem': 'SMS-modem', 'whatsapp': 'WhatsApp', 'queue': 'Leveringskø', 'disk': 'Serverdisk', 'sms_storage': 'SMS-lager', 'offsite': 'Ekstern backup'}


def connection_label(value):
    state = str(value)
    transport, separator, state_value = state.partition(':')
    names = {'online': 'Online', 'ready': 'Forbundet', 'offline': 'Offline', 'missing': 'Session mangler', 'unknown': 'Ukendt', 'initializing': 'Starter', 'connecting': 'Forbinder', 'stale': 'Forældet status', 'degraded': 'Kræver opmærksomhed', 'recovering': 'Genopretter'}
    if separator:
        return {'usb': 'USB', 'cudy': 'LT300'}.get(transport, 'Ukendt modem') + ' · ' + names.get(state_value, state_value)
    return names.get(state, state)


app.jinja_env.globals['connection_label'] = connection_label


class ConnectionState(db.Model):
    component = db.Column(db.String(20), primary_key=True)
    state = db.Column(db.String(40), nullable=False)
    checked_at = db.Column(db.DateTime, nullable=False)
    changed_at = db.Column(db.DateTime, nullable=False)


class ConnectionChange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    component = db.Column(db.String(20), nullable=False, index=True)
    previous = db.Column(db.String(40))
    state = db.Column(db.String(40), nullable=False)
    at = db.Column(db.DateTime, nullable=False, index=True)


class OperatingIncident(db.Model):
    key = db.Column(db.String(20), primary_key=True)
    bad_since = db.Column(db.DateTime)
    notified_at = db.Column(db.DateTime)
    attempted_at = db.Column(db.DateTime)
    recovery_pending = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.String(255))


class StorageReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    checked_at = db.Column(db.DateTime, nullable=False)
    free_bytes = db.Column(db.BigInteger)
    total_bytes = db.Column(db.BigInteger)
    database_bytes = db.Column(db.BigInteger)
    sms_used = db.Column(db.Integer)
    sms_total = db.Column(db.Integer)


def sms_capacity(value):
    # CPMS accepts named stores or a numeric response; examine the read store.
    if not isinstance(value, str):
        return None, None
    match = re.search(r'\+CPMS:\s*(?:"[A-Za-z0-9]+"\s*,\s*)?(\d+)\s*,\s*(\d+)', value)
    if match:
        used, total = map(int, match.groups())
        if 0 <= used <= total <= 100000 and total:
            return used, total
    return None, None


def storage_reading(modem):
    path = Path(db.engine.url.database).resolve()
    usage = shutil.disk_usage(path.parent)
    size = sum(p.stat().st_size for p in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')) if p.exists())
    used, total = sms_capacity(modem.get('storage')) if modem.get('state') == 'online' else (None, None)
    return StorageReading(checked_at=base.utcnow(), free_bytes=usage.free, total_bytes=usage.total, database_bytes=size, sms_used=used, sms_total=total)


def enabled():
    return ops.setting('alerts_enabled', 'false') == 'true'


def alert_configuration():
    channel = os.getenv('SMS_WHATSAPP_ALERT_CHANNEL', 'disabled').lower()
    if channel == 'webhook':
        try:
            address = urllib.parse.urlsplit(os.getenv('SMS_WHATSAPP_ALERT_WEBHOOK_URL', ''))
            valid = address.scheme == 'https' and bool(address.hostname) and not address.username and not address.password and not address.fragment
        except ValueError:
            valid = False
    elif channel == 'smtp':
        sender = os.getenv('SMS_WHATSAPP_ALERT_FROM', '')
        target = os.getenv('SMS_WHATSAPP_ALERT_TO', '')
        address_pattern = r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+'
        valid = bool(os.getenv('SMS_WHATSAPP_ALERT_SMTP_HOST')) and bool(re.fullmatch(address_pattern, sender)) and bool(re.fullmatch(address_pattern, target)) and os.getenv('SMS_WHATSAPP_ALERT_SMTP_MODE', 'starttls') in ('starttls', 'ssl')
        try:
            valid = valid and 1 <= int(os.getenv('SMS_WHATSAPP_ALERT_SMTP_PORT', '587')) <= 65535
        except ValueError:
            valid = False
    else:
        valid = False
    return {'channel': channel if channel in ('webhook', 'smtp') else 'disabled', 'ready': bool(valid), 'enabled': enabled()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward bearer credentials to a redirect target.


def send_notice(key, recovered):
    config = alert_configuration()
    if not config['ready']:
        raise ValueError('Driftskanalen er ikke konfigureret')
    title = 'SBR Pager · ' + ('Forbindelse/status genoprettet' if recovered else 'Drift kræver opmærksomhed')
    message = '[SBR-SYSTEM] ' + LABELS[key] + (': normal status er målt igen.' if recovered else ': kontrollér Driftsværn og Forbindelser i Pager.')
    if config['channel'] == 'webhook':
        headers = {'Content-Type': 'application/json'}
        token = os.getenv('SMS_WHATSAPP_ALERT_WEBHOOK_TOKEN', '')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        payload = json.dumps({'title': title, 'message': message, 'component': key, 'recovered': recovered}).encode()
        req = urllib.request.Request(os.environ['SMS_WHATSAPP_ALERT_WEBHOOK_URL'], data=payload, headers=headers, method='POST')
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=8) as response:
            if not 200 <= response.status < 300:
                raise ValueError('Driftskanalen afviste beskeden')
    else:
        mail = EmailMessage()
        mail['From'], mail['To'], mail['Subject'] = os.environ['SMS_WHATSAPP_ALERT_FROM'], os.environ['SMS_WHATSAPP_ALERT_TO'], title
        mail.set_content(message)
        host = os.environ['SMS_WHATSAPP_ALERT_SMTP_HOST']
        mode = os.getenv('SMS_WHATSAPP_ALERT_SMTP_MODE', 'starttls')
        port = int(os.getenv('SMS_WHATSAPP_ALERT_SMTP_PORT', '465' if mode == 'ssl' else '587'))
        context = ssl.create_default_context()
        connection = smtplib.SMTP_SSL(host, port, timeout=8, context=context) if mode == 'ssl' else smtplib.SMTP(host, port, timeout=8)
        with connection as server:
            if mode == 'starttls':
                server.ehlo(); server.starttls(context=context); server.ehlo()
            username = os.getenv('SMS_WHATSAPP_ALERT_SMTP_USERNAME', '')
            if username:
                server.login(username, os.getenv('SMS_WHATSAPP_ALERT_SMTP_PASSWORD', ''))
            server.send_message(mail)


def record_connection(component, state, now):
    state = str(state)[:40]
    current = db.session.get(ConnectionState, component)
    if current is None:
        current = ConnectionState(component=component, state=state, checked_at=now, changed_at=now)
        db.session.add(current)
        db.session.add(ConnectionChange(component=component, state=state, at=now))
    elif current.state != state:
        db.session.add(ConnectionChange(component=component, previous=current.state, state=state, at=now))
        current.state, current.changed_at = state, now
    current.checked_at = now


def update_incident(key, bad, now):
    if bad is None:
        return  # A deliberate maintenance pause does not prove queue recovery.
    row = db.session.get(OperatingIncident, key)
    if row is None:
        row = OperatingIncident(key=key, recovery_pending=False)
        db.session.add(row)
    if bad:
        if not row.bad_since:
            row.bad_since, row.notified_at, row.attempted_at, row.error = now, None, None, None
        row.recovery_pending = False
    elif row.bad_since:
        row.bad_since = None
        row.recovery_pending = bool(row.notified_at)
        row.attempted_at = None
        row.error = None


def deliver_notices(now):
    if not enabled() or not alert_configuration()['ready']:
        return
    delay = ops.number_setting('alert_delay_seconds', 120, 30, 3600)
    repeat = ops.number_setting('alert_repeat_minutes', 60, 15, 1440) * 60
    for row in OperatingIncident.query.all():
        if row.key == 'queue' and ops.runtime_mode()['name'] != 'normal':
            continue
        if row.key == 'offsite':
            import offsite_backup
            if not offsite_backup.enabled():
                continue
        bad = row.bad_since and (now - delivery._aware(row.bad_since)).total_seconds() >= delay
        due = bad and (not row.notified_at or (now - delivery._aware(row.notified_at)).total_seconds() >= repeat)
        if not (due or row.recovery_pending):
            continue
        if row.attempted_at and (now - delivery._aware(row.attempted_at)).total_seconds() < 300:
            continue
        recovered = bool(row.recovery_pending and not row.bad_since)
        key = row.key
        row.attempted_at = now
        db.session.commit()  # Persist rate limiting before any external request.
        try:
            send_notice(key, recovered)
            row.error = None
            if recovered:
                row.recovery_pending = False
            else:
                row.notified_at = now
        except Exception:
            # HTTP exceptions can contain tokens in URLs. Never persist/log them.
            row.error = 'Driftsbesked kunne ikke afleveres. Kontrollér kanal og forbindelse.'
            log.warning('Driftsbesked for %s kunne ikke afleveres', key)
        db.session.commit()


def monitor_once():
    import dashboard_app
    now = base.utcnow()
    gateway = dashboard_app.gateway_status()
    modem = gateway.get('modem') or {}
    wa = base.openwa_status()
    internet = diagnostics.internet_status()
    queue = delivery.queue_snapshot()
    for key, state in (('internet', internet.get('state', 'unknown')), ('modem', str(modem.get('transport', 'unknown')) + ':' + str(modem.get('state', 'unknown'))), ('whatsapp', wa.get('state', 'unknown'))):
        record_connection(key, state, now)
    for key, bad in (('internet', internet.get('state') != 'online'), ('modem', modem.get('state') != 'online'), ('whatsapp', wa.get('state') != 'ready')):
        update_incident(key, bad, now)
    paused = ops.runtime_mode()['name'] != 'normal'
    queue_bad = queue['active'] >= ops.number_setting('alert_queue_count', 20, 1, 1000) or (queue['oldestMinutes'] is not None and queue['oldestMinutes'] >= 5) or bool(db.session.query(base.WhatsAppDelivery.id).filter_by(status='held').first())
    update_incident('queue', None if paused else queue_bad, now)
    try:
        reading = storage_reading(modem)
        db.session.add(reading)
        update_incident('disk', reading.free_bytes < ops.number_setting('disk_min_mb', 512, 64, 102400) * 1024 * 1024 or reading.free_bytes / reading.total_bytes * 100 < ops.number_setting('disk_min_percent', 10, 1, 50), now)
        update_incident('sms_storage', reading.sms_used / reading.sms_total * 100 >= 80 if reading.sms_total else None, now)
    except OSError:
        update_incident('disk', True, now)
    import offsite_backup
    offsite = offsite_backup.status_snapshot()
    update_incident('offsite', (offsite['state'] in ('failed', 'stale', 'unconfigured')) if offsite['enabled'] else None, now)
    ConnectionChange.query.filter(ConnectionChange.at < now - timedelta(days=90)).delete()
    old_changes = db.session.query(ConnectionChange.id).order_by(ConnectionChange.id.desc()).offset(10000).subquery()
    ConnectionChange.query.filter(ConnectionChange.id.in_(db.select(old_changes.c.id))).delete(synchronize_session=False)
    latest = db.session.query(StorageReading.id).order_by(StorageReading.id.desc()).first()
    if latest:
        StorageReading.query.filter(StorageReading.id != latest[0]).delete(synchronize_session=False)
    db.session.commit()
    # Let OpenWA and the modem initialize before warning after a restart.
    if now - ops.BOOTED_AT >= timedelta(minutes=5):
        deliver_notices(now)


@app.get('/driftsvaern')
@base.login_required
def resilience_page():
    import offsite_backup
    return render_template('resilience.html', title='Driftsværn', channel=alert_configuration(), states=ConnectionState.query.all(), incidents=OperatingIncident.query.all(), storage=StorageReading.query.order_by(StorageReading.id.desc()).first(), labels=LABELS, offsite=offsite_backup.status_snapshot(), get_setting=ops.number_setting)


@app.post('/driftsvaern')
@admin_required
@delivery.serialized
def resilience_controls():
    base.check_csrf()
    wants_alerts = request.form.get('alerts_enabled') == '1'
    if wants_alerts and not alert_configuration()['ready']:
        flash('Konfigurér først en gyldig, separat driftskanal på serveren.', 'error')
        return redirect(url_for('resilience_page'))
    values = {}
    bounds = {'alert_delay_seconds': (30, 3600), 'alert_repeat_minutes': (15, 1440), 'alert_queue_count': (1, 1000), 'disk_min_mb': (64, 102400), 'disk_min_percent': (1, 50)}
    try:
        for key, (minimum, maximum) in bounds.items():
            value = int(request.form[key])
            if not minimum <= value <= maximum:
                raise ValueError()
            values[key] = str(value)
    except (KeyError, ValueError):
        flash('Kontrollér grænserne i formularen.', 'error')
        return redirect(url_for('resilience_page'))
    values['alerts_enabled'] = 'true' if wants_alerts else 'false'
    for key, value in values.items():
        ops.set_setting(key, value)
    db.session.commit()
    ops.record_audit('Driftsværn ændret', values)
    flash('Driftsvalg gemt. Ingen testbesked er sendt.')
    _wake.set()
    return redirect(url_for('resilience_page'))


@app.get('/forbindelseshistorik')
@base.login_required
def connection_history():
    component = request.args.get('component', '')
    query = ConnectionChange.query
    if component in ('internet', 'modem', 'whatsapp'):
        query = query.filter_by(component=component)
    return render_template('connection_history.html', title='Forbindelseshistorik', rows=query.order_by(ConnectionChange.id.desc()).limit(200).all(), labels=LABELS, states=ConnectionState.query.all())


def monitor_worker():
    while not _stop.is_set():
        try:
            with app.app_context():
                monitor_once()
        except Exception:
            # Monitoring is isolated from delivery, including session rollback.
            log.warning('Driftsmåling kunne ikke gennemføres')
        _wake.wait(60)
        _wake.clear()


def start_monitor():
    global _started
    if _started or os.getenv('SMS_WHATSAPP_MONITOR_WORKER', 'true').lower() != 'true' or os.getenv('SMS_WHATSAPP_RETRY_WORKER', 'true').lower() != 'true':
        return
    _started = True
    threading.Thread(target=monitor_worker, name='pager-monitor', daemon=True).start()


with app.app_context():
    db.create_all()
