"""Personal operator accounts; retain the environment login for recovery."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta
from functools import wraps

from flask import abort, flash, g, has_request_context, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
import operations as ops

app, base, db = ops.app, ops.base, ops.db
app.config['SESSION_COOKIE_NAME'] = 'sbr_pager_operator_session'
# Older images only understand the old "session" cookie and shared admin login.
# A read-only cookie must never become administrator access on image rollback.
_environment_username = base.admin_username


class OperatorAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(12), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    version = db.Column(db.Integer, nullable=False, default=1)


class LoginFailure(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(64), nullable=False, index=True)
    username = db.Column(db.String(64), nullable=False, index=True)
    at = db.Column(db.DateTime, nullable=False, default=base.utcnow, index=True)


def environment_fingerprint():
    return hmac.new(app.secret_key.encode(), (_environment_username() + '\0' + os.getenv('SMS_WHATSAPP_ADMIN_PASSWORD', '')).encode(), hashlib.sha256).hexdigest()


def establish_session(account=None):
    session.clear()
    if hasattr(request, '_pager_identity'):
        delattr(request, '_pager_identity')
    session.update(admin_authenticated=True, csrf_token=secrets.token_urlsafe(32))
    session['auth_epoch'] = ops.setting('operator_auth_epoch', '')
    if account is None:
        session.update(auth_kind='environment', auth_fingerprint=environment_fingerprint())
    else:
        session.update(auth_kind='personal', auth_user_id=account.id, auth_version=account.version)


def identity():
    if not has_request_context() or not session.get('admin_authenticated'):
        return None
    if hasattr(request, '_pager_identity'):
        return request._pager_identity
    result = None
    if session.get('auth_epoch', '') != ops.setting('operator_auth_epoch', ''):
        request._pager_identity = None
        return None
    if session.get('auth_kind') == 'environment':
        if os.getenv('SMS_WHATSAPP_ADMIN_PASSWORD') and hmac.compare_digest(str(session.get('auth_fingerprint', '')), environment_fingerprint()):
            result = {'name': _environment_username() + ' (miljølogin)', 'role': 'admin', 'id': None}
    elif session.get('auth_kind') == 'personal':
        identifier = session.get('auth_user_id')
        row = db.session.get(OperatorAccount, identifier) if isinstance(identifier, int) else None
        if row and row.active and row.role in ('admin', 'viewer') and row.version == session.get('auth_version'):
            result = {'name': row.username, 'role': row.role, 'id': row.id}
    request._pager_identity = result
    return result


def logged_in():
    return identity() is not None


def current_actor():
    person = identity()
    return person['name'] if person else 'System'


def admin_required(fn):
    @wraps(fn)
    @base.login_required
    def wrapped(*args, **kwargs):
        if identity()['role'] != 'admin':
            abort(403, 'Denne handling kræver administratoradgang')
        return fn(*args, **kwargs)
    return wrapped


@app.before_request
def enforce_roles():
    person = identity()
    if session.get('admin_authenticated') and not person:
        session.clear()
    if person and person['role'] == 'viewer':
        if (request.method not in ('GET', 'HEAD', 'OPTIONS') and request.endpoint != 'login') or request.endpoint in ('download_backup', 'diagnostic_export'):
            abort(403, 'Du har læseadgang og kan ikke ændre eller sende')


def login():
    status = 200
    if request.method == 'POST':
        base.check_csrf()
        username = request.form.get('username', '').strip()[:80]
        password = request.form.get('password', '')
        source = hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
        name_key = hashlib.sha256(username.casefold().encode()).hexdigest()
        cutoff = base.utcnow() - timedelta(minutes=10)
        failures = LoginFailure.query.filter(LoginFailure.source == source, LoginFailure.at >= cutoff)
        if failures.count() >= 30 or failures.filter_by(username=name_key).count() >= 8:
            flash('For mange loginforsøg. Prøv igen om ti minutter.', 'error')
            status = 429
        else:
            row = OperatorAccount.query.filter_by(username=username.casefold()).first()
            personal = row and row.active and len(password) <= 256 and check_password_hash(row.password_hash, password)
            environment = len(password) <= 256 and hmac.compare_digest(username.encode(), _environment_username().encode()) and base.password_matches(password)
            if personal or environment:
                establish_session(row if personal else None)
                LoginFailure.query.filter_by(source=source, username=name_key).delete()
                db.session.commit()
                next_path = request.args.get('next', '')
                # Browsers normalize backslashes into network-path redirects.
                safe = next_path.startswith('/') and not next_path.startswith('//') and '\\' not in next_path and not any(ord(c) < 32 for c in next_path)
                return redirect(next_path if safe else url_for('dashboard'))
            LoginFailure.query.filter(LoginFailure.at < base.utcnow() - timedelta(days=1)).delete()
            db.session.add(LoginFailure(source=source, username=name_key))
            db.session.commit()
            flash('Forkert brugernavn eller adgangskode.', 'error')
    return render_template_string(base.LOGIN_HTML, title='Login'), status


@app.get('/administratorer')
@admin_required
def operator_accounts():
    return render_template('operator_accounts.html', title='Administratorer og læseadgang', rows=OperatorAccount.query.order_by(OperatorAccount.username).all())


def account_values(new=False):
    username = request.form.get('username', '').strip().casefold()
    role = request.form.get('role')
    password = request.form.get('password', '')
    if not re.fullmatch(r'[a-z0-9æøå][a-z0-9æøå._-]{2,79}', username) or username == _environment_username().casefold():
        abort(400, 'Brug 3–80 tegn i et unikt brugernavn; miljølogin-navnet er reserveret')
    if role not in ('admin', 'viewer') or (new or password) and not 12 <= len(password) <= 256:
        abort(400, 'Vælg en rolle og en adgangskode på 12–256 tegn')
    return username, role, password


@app.post('/administratorer')
@admin_required
@ops.delivery.serialized
def create_operator():
    base.check_csrf()
    username, role, password = account_values(new=True)
    if OperatorAccount.query.filter_by(username=username).first():
        abort(409, 'Brugernavnet er allerede i brug')
    row = OperatorAccount(username=username, role=role, password_hash=generate_password_hash(password, method='scrypt'))
    db.session.add(row)
    db.session.commit()
    ops.record_audit('Opret operatørkonto', {'id': row.id, 'brugernavn': username, 'rolle': role})
    flash('Personlig konto oprettet. Del adgangskoden sikkert med personen.')
    return redirect(url_for('operator_accounts'))


@app.post('/administratorer/<int:identifier>')
@admin_required
@ops.delivery.serialized
def update_operator(identifier):
    base.check_csrf()
    row = db.get_or_404(OperatorAccount, identifier)
    username, role, password = account_values()
    active = request.form.get('active') == '1'
    if OperatorAccount.query.filter(OperatorAccount.username == username, OperatorAccount.id != row.id).first():
        abort(409, 'Brugernavnet er allerede i brug')
    if row.active and row.role == 'admin' and (not active or role != 'admin') and OperatorAccount.query.filter_by(active=True, role='admin').count() <= 1:
        abort(409, 'Den sidste aktive personlige administrator skal bevares')
    before = {'brugernavn': row.username, 'rolle': row.role, 'aktiv': row.active}
    row.username, row.role, row.active = username, role, active
    if password:
        row.password_hash = generate_password_hash(password, method='scrypt')
    row.version += 1  # Invalidate existing sessions after any account change.
    actor = current_actor()
    db.session.commit()
    ops.record_audit('Ændr operatørkonto', {'id': row.id, 'før': before, 'efter': {'brugernavn': username, 'rolle': role, 'aktiv': active}, 'adgangskode_ændret': bool(password)}, actor=actor)
    flash('Konto opdateret. Tidligere logins til kontoen er afsluttet.')
    return redirect(url_for('operator_accounts'))


@app.context_processor
def account_context():
    return {'operator_identity': identity(), 'operator_readonly': bool(identity() and identity()['role'] == 'viewer')}


with app.app_context():
    db.create_all()
base.logged_in = logged_in
base.audit_actor = current_actor
app.view_functions['login'] = login
