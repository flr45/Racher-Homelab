"""Encrypted copies to an explicitly mounted second-machine backup directory."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import tempfile
import time
import restore_db
from datetime import timedelta

from flask import flash, redirect, request, url_for
import operations as ops
import encrypted_backup
from operator_accounts import admin_required

app, base, db = ops.app, ops.base, ops.db
MARKER = 'SBR-PAGER-OFFSITE-v1'
_wake = threading.Event()
_started = False


class OffsiteTransfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    backup_name = db.Column(db.String(64), nullable=False)
    key_id = db.Column(db.String(16), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False, default=base.utcnow)
    completed_at = db.Column(db.DateTime)
    state = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.String(255), nullable=False)
    ciphertext_sha256 = db.Column(db.String(64))


def enabled():
    return ops.setting('offsite_enabled', 'false') == 'true'


def configured():
    # Readiness of the actual remote mount is checked only by the copy worker.
    return bool(os.getenv('SMS_WHATSAPP_OFFSITE_DIR') and os.getenv('SMS_WHATSAPP_OFFSITE_KEY_FILE'))


def status_snapshot():
    latest = OffsiteTransfer.query.order_by(OffsiteTransfer.id.desc()).first()
    state = 'disabled' if not enabled() else 'unconfigured' if not configured() else 'pending' if not latest else latest.state
    if enabled() and configured() and latest:
        if latest.state == 'copying' and base.utcnow() - ops.delivery._aware(latest.started_at) > timedelta(minutes=10):
            state = 'stale'
        elif latest.state == 'passed' and base.utcnow() - ops.delivery._aware(latest.completed_at) > timedelta(hours=36):
            state = 'stale'
    return {'enabled': enabled(), 'configured': configured(), 'state': state, 'latest': latest}


def destination():
    root = Path(os.environ['SMS_WHATSAPP_OFFSITE_DIR'])
    marker = root / '.sbr-pager-offsite'
    if not root.is_absolute() or not root.is_dir() or marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 100 or marker.read_text().strip() != MARKER:
        raise ValueError('Backupdestinationens mount-markør mangler')
    # Reject writing into the application's own backup/data directories.
    resolved = root.resolve()
    data = Path(db.engine.url.database).resolve().parent
    backup_root = ops.backup_directory().resolve()
    if resolved == data or data in resolved.parents or resolved == backup_root or backup_root in resolved.parents:
        raise ValueError('Destinationen skal ligge uden for driftsdata')
    return resolved


def verify_external_archive(target, key_path):
    """Decrypt the remote copy and exercise the same offline restore on scratch DBs."""
    with tempfile.TemporaryDirectory(prefix='pager-offsite-restore-') as temporary:
        root = Path(temporary)
        extracted = encrypted_backup.decrypt_to_directory(target, key_path, root / 'unpacked')
        ops.validated_config(json.loads((extracted / 'pager.json').read_text()))
        current = root / 'current.sqlite'
        with sqlite3.connect(Path(db.engine.url.database).resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(current) as snapshot:
            deadline = time.monotonic() + 30
            def progress(*args):
                if time.monotonic() > deadline:
                    raise TimeoutError("Gendannelseskontrol tog for lang tid")
            source.backup(snapshot, pages=256, progress=progress)
        restore_db.restore(extracted / 'pager.sqlite', current)
        with sqlite3.connect(current) as restored:
            if restored.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or restored.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Gendannelseskontrol fejlede')
            if restored.execute("SELECT count(*) FROM whats_app_delivery WHERE status IN ('pending','retrying','held','sending')").fetchone()[0]:
                raise ValueError('Gendannelsen beholdt aktive jobs')


def transfer_once():
    if not enabled() or not configured():
        return
    backups = ops.backup_list()
    if not backups:
        return
    name = backups[0]['name']
    key_path = os.environ['SMS_WHATSAPP_OFFSITE_KEY_FILE']
    # Never include the key or its path in error messages, archives or audit.
    key_id = hashlib.sha256(key_path.encode()).hexdigest()[:16]
    try:
        if Path(key_path).stat().st_size <= 256:
            key_id = hashlib.sha256(Path(key_path).read_bytes().strip()).hexdigest()[:16]
    except OSError:
        pass
    previous = OffsiteTransfer.query.filter_by(backup_name=name, key_id=key_id).order_by(OffsiteTransfer.id.desc()).first()
    if previous and (previous.state == 'passed' or base.utcnow() - ops.delivery._aware(previous.started_at) < timedelta(hours=1)):
        return
    row = OffsiteTransfer(backup_name=name, key_id=key_id, state='copying', detail='Krypteret kopiering i gang')
    db.session.add(row)
    db.session.commit()
    temporary = None
    try:
        crypt = encrypted_backup.cipher(key_path)
        root = destination()
        with ops._backup_lock:
            database, configuration = ops.backup_path(name, '.sqlite'), ops.backup_path(name, '.json')
            ops.validated_config(json.loads(configuration.read_text()))
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source:
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or source.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Backupintegritet')
            payload = encrypted_backup.package(database, configuration, name)
        target = root / (name + '-' + key_id + '.fernet')
        if target.exists():
            if target.is_symlink() or target.stat().st_size > (encrypted_backup.MAX_BYTES + 8192) * 2 or crypt.decrypt(target.read_bytes()) != payload:
                raise ValueError('Eksisterende ekstern backup passer ikke')
        else:
            encrypted = crypt.encrypt(payload)
            temporary = root / ('.pager-transfer-' + secrets.token_hex(12) + '.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            # No remote I/O holds a delivery/database lock.
            if crypt.decrypt(temporary.read_bytes()) != payload:
                raise ValueError('Kopiens integritet')
            temporary.replace(target)
        verify_external_archive(target, key_path)
        row.ciphertext_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        row.state, row.detail = 'passed', 'Krypteret kopi læst tilbage og prøvegendannet isoleret; ingen driftsdata ændret'
    except Exception:
        row.state, row.detail = 'failed', 'Kopiering fejlede. Kontrollér mount-markør, nøgle, rettigheder, forbindelse og grænsen på 32 MiB.'
    finally:
        if temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Any leftover temporary file contains ciphertext only.
    row.completed_at = base.utcnow()
    db.session.commit()
    # Keep at most 100 transfer records; remote backup files are never pruned.
    cutoff = db.session.query(OffsiteTransfer.id).order_by(OffsiteTransfer.id.desc()).offset(100).subquery()
    OffsiteTransfer.query.filter(OffsiteTransfer.id.in_(db.select(cutoff.c.id))).delete(synchronize_session=False)
    db.session.commit()


@app.post('/driftsvaern/ekstern-backup')
@admin_required
@ops.delivery.serialized
def offsite_controls():
    base.check_csrf()
    value = request.form.get('enabled') == '1'
    if value and not configured():
        flash('Tilslut først en backupmappe på den anden maskine og en separat nøglefil på serveren.', 'error')
    else:
        ops.set_setting('offsite_enabled', 'true' if value else 'false')
        db.session.commit()
        ops.record_audit('Ekstern backup ændret', {'aktiv': value})
        _wake.set()
        flash('Ekstern backup aktiveret.' if value else 'Ekstern backup deaktiveret.')
    return redirect(url_for('resilience_page'))


def copy_worker():
    while True:
        try:
            with app.app_context():
                transfer_once()
        except Exception:
            # An unreachable filesystem or key must not stop delivery/monitoring.
            pass
        _wake.wait(60)
        _wake.clear()


def start_worker():
    global _started
    if _started or os.getenv('SMS_WHATSAPP_RETRY_WORKER', 'true').lower() != 'true' or os.getenv('SMS_WHATSAPP_MONITOR_WORKER', 'true').lower() != 'true':
        return
    _started = True
    threading.Thread(target=copy_worker, name='pager-offsite-copy', daemon=True).start()


with app.app_context():
    db.create_all()
