"""Exercise interrupted updates against real files, SQLite and backup archives."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('openwa_update', ROOT / 'scripts/update-openwa.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)
VOLUME = 'sms-whatsapp_sms_whatsapp_openwa_data'
SESSION = 'existing-session-uuid'
OLD = 'sha256:' + 'a' * 64
NEW = 'sha256:' + 'b' * 64
DIGEST = 'sha256:' + 'c' * 64


class Host(update.Updater):
    def __init__(self, root, fault=None):
        super().__init__(root, ready_seconds=0)
        self.sudo = []
        self.fault = fault
        self.calls = []
        self.timer = True
        self.live_image = OLD
        self.running = {update.OPENWA: True, update.PAGER: True}
        self.status = 'ready'
        self.data = root / 'volume'
        self.data.mkdir()
        self.profile = self.data / 'sessions' / ('session-' + SESSION)
        self.profile.mkdir(parents=True)
        (self.profile / 'credential').write_text('saved-login')
        (self.data / '.api-key').write_text('synthetic-api-key')
        with sqlite3.connect(self.data / 'main.sqlite') as db:
            db.execute('CREATE TABLE settings (original TEXT)')
            db.execute("INSERT INTO settings VALUES ('old-schema')")
        self.root.joinpath('.git').mkdir()

    def inspect(self, name):
        service = 'openwa' if name == update.OPENWA else 'sms-whatsapp'
        return {'Id': 'openwa-container' if service == 'openwa' else 'pager-container',
                'Image': self.live_image if service == 'openwa' else 'pager-image',
                'State': {'Running': self.running[name]},
                'Mounts': [{'Type': 'volume', 'Name': 'wrong-volume' if self.fault == 'volume' else VOLUME,
                            'Destination': '/app/data'}] if service == 'openwa' else [],
                'Config': {'Env': ['OPENWA_API_KEY=synthetic-api-key', 'OPENWA_SESSION_ID=' + SESSION],
                           'Labels': {'com.docker.compose.project': 'sms-whatsapp',
                                      'com.docker.compose.service': service,
                                      'com.docker.compose.project.config_files': str(self.compose_file)}}}

    def probe(self):
        return {'status': self.status, 'engineLoaded': True}

    def compose(self, *args, override=None):
        self.calls.append(('compose', *args))
        if args == ('config', '--format', 'json'):
            settings = dict(line.split('=', 1) for line in self.env_file.read_text().splitlines() if '=' in line)
            assert '    image: ' + update.IMAGE_SETTING in self.compose_file.read_text()
            config = {'services': {'openwa': {
                'image': settings['SMS_WHATSAPP_OPENWA_IMAGE'],
                'environment': {'AUTO_START_SESSIONS': settings['SMS_WHATSAPP_OPENWA_AUTO_START_SESSIONS']},
                'volumes': [{'source': 'sms_whatsapp_openwa_data', 'target': '/app/data'}],
            }}, 'volumes': {'sms_whatsapp_openwa_data': {
                'name': 'unexpected' if self.fault == 'resolved_volume' else VOLUME}}}
            return subprocess.CompletedProcess(args, 0, json.dumps(config).encode(), b'')
        if args[0] == 'up':
            assert self.running[update.PAGER] is False
            if override:
                self.live_image = OLD
                self.status = 'ready'
            else:
                self.live_image = NEW
                self.running[update.OPENWA] = True
                # Model a schema migration and changed browser profile, even if
                # the app fails later. A rollback of just the image is unsafe.
                with sqlite3.connect(self.data / 'main.sqlite') as db:
                    db.execute('ALTER TABLE settings ADD COLUMN migrated TEXT')
                (self.profile / 'credential').write_text('new-profile')
                (self.data / 'new-version-only').write_text('new')
                if self.fault in {'start', 'rollback_stop'}:
                    raise update.UpdateError('synthetic failed startup')
                if self.fault == 'timeout':
                    self.status = 'initializing'
                elif self.fault == 'qr':
                    self.status = 'qr_ready'
                self.ready_seconds = 0 if self.fault == 'timeout' else 1
            self.running[update.OPENWA] = True
        return subprocess.CompletedProcess(args, 0, b'', b'')

    def command(self, *args, input=None, input_file=None, output=None, check=True, **kwargs):
        self.calls.append(args)
        payload, code = b'', 0
        if args[:2] == ('git', 'rev-parse'):
            payload = str(self.root / '.git').encode()
        elif args[:3] == ('systemctl', 'is-active', '--quiet'):
            code = 0 if args[-1] == update.TIMER and self.timer else 3
        elif args[:2] == ('systemctl', 'stop'):
            self.timer = False
        elif args[:2] == ('systemctl', 'start'):
            self.timer = True
        elif args[:2] == ('docker', 'ps'):
            payload = b'openwa-container\n' if self.running[update.OPENWA] else b''
        elif args[:2] == ('docker', 'pull'):
            assert all(self.running.values())
            if self.fault == 'pull':
                raise update.UpdateError('synthetic registry unavailable')
            if self.fault == 'concurrent_edit':
                self.compose_file.write_text(self.compose_file.read_text() + '\n# operator edit\n')
        elif args[:3] == ('docker', 'image', 'inspect'):
            payload = json.dumps([{'Id': NEW, 'RepoDigests': ['ghcr.io/rmyndharis/openwa@' + DIGEST]}]).encode()
        elif args[:2] == ('docker', 'stop'):
            if not (self.fault == 'rollback_stop' and args[-1] == update.OPENWA and self.live_image == NEW):
                self.running[args[-1]] = False
        elif args[:2] == ('docker', 'start'):
            self.running[args[-1]] = True
        elif args[:2] == ('docker', 'run') and '/snapshot' in ' '.join(args):
            assert not self.running[update.OPENWA]
            with tarfile.open(fileobj=output, mode='w:') as archive:
                if self.fault == 'backup':
                    marker = tarfile.TarInfo('incomplete')
                    archive.addfile(marker, io.BytesIO())
                else:
                    archive.add(self.data, arcname='.')
        elif args[:2] == ('docker', 'run') and args[-1] == update.RESTORE:
            assert not self.running[update.OPENWA]
            assert not self.running[update.PAGER]
            for path in self.data.iterdir():
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            with tarfile.open(fileobj=input_file) as archive:
                archive.extractall(self.data, filter='fully_trusted')
        elif args[:2] == ('docker', 'logs'):
            output.write(b'pre-update diagnostic log\n')
        return subprocess.CompletedProcess(args, code, payload, b'')


@pytest.fixture
def host(tmp_path):
    compose = tmp_path / 'compose/sms-whatsapp/compose.yml'
    compose.parent.mkdir(parents=True)
    # Start from the older live host template, not just the updated repo file.
    compose.write_text((ROOT / 'compose/sms-whatsapp/compose.yml').read_text().replace(
        update.IMAGE_SETTING, 'ghcr.io/rmyndharis/openwa:latest').replace('    stop_grace_period: 75s\n', ''))
    (tmp_path / '.env').write_text('UNCHANGED=synthetic-private-value\n')
    return Host(tmp_path)


def original_files(host):
    return host.env_file.read_bytes(), host.compose_file.read_bytes()


def assert_original_data(host):
    assert (host.profile / 'credential').read_text() == 'saved-login'
    assert (host.data / '.api-key').read_text() == 'synthetic-api-key'
    assert not (host.data / 'new-version-only').exists()
    with sqlite3.connect(host.data / 'main.sqlite') as db:
        assert [row[1] for row in db.execute('PRAGMA table_info(settings)')] == ['original']
        assert db.execute('SELECT original FROM settings').fetchone() == ('old-schema',)


def test_update_backs_up_profile_and_db_then_pins_image_and_resumes(host, capsys):
    before = original_files(host)
    host.run()
    assert host.live_image == NEW
    assert all(host.running.values()) and host.timer
    assert host.state['stage'] == 'installed'
    assert host.state['session_status'] == 'ready'
    assert host.env_file.read_text().endswith('SMS_WHATSAPP_OPENWA_AUTO_START_SESSIONS=true\n')
    assert update.IMAGE + '@' + DIGEST in host.env_file.read_text()
    assert (host.backup / 'env.before').read_bytes() == before[0]
    assert (host.backup / 'compose.before.yml').read_bytes() == before[1]
    assert (host.backup / 'update-openwa.py').is_file()
    assert (host.backup / 'env.before').stat().st_mode & 0o077 == 0
    assert host.backup.stat().st_mode & 0o077 == 0
    assert 'synthetic-private-value' not in capsys.readouterr().out
    assert not any('/logout' in str(call) or '/messages' in str(call) for call in host.calls)


@pytest.mark.parametrize('fault', ['start', 'timeout'])
def test_failed_migration_restores_database_auth_and_files_together(host, fault):
    before = original_files(host)
    host.fault = fault
    with pytest.raises(update.UpdateError):
        host.run()
    assert host.live_image == OLD
    assert all(host.running.values()) and host.timer
    assert original_files(host) == before
    assert_original_data(host)
    assert host.state['stage'] == 'rolled_back'
    assert list(host.backup.glob('openwa-before-rollback-*.tar'))


@pytest.mark.parametrize('fault', ['volume', 'pull', 'backup', 'resolved_volume'])
def test_failures_before_replacement_preserve_original_installation(host, fault):
    before = original_files(host)
    host.fault = fault
    with pytest.raises(update.UpdateError):
        host.run()
    assert host.live_image == OLD
    assert all(host.running.values()) and host.timer
    assert original_files(host) == before
    assert_original_data(host)
    assert not any(call[0:2] == ('compose', 'up') for call in host.calls)


def test_concurrent_operator_edit_is_not_overwritten(host):
    host.fault = 'concurrent_edit'
    with pytest.raises(update.UpdateError, match='ændret'):
        host.run()
    assert host.compose_file.read_text().endswith('# operator edit\n')
    assert host.live_image == OLD and host.timer and all(host.running.values())


def test_qr_required_is_reported_without_login_deletion_or_restart_loop(host):
    host.fault = 'qr'
    host.run()
    assert host.live_image == NEW and host.state['session_status'] == 'qr_ready'
    assert host.timer and all(host.running.values())
    assert len([call for call in host.calls if call[:2] == ('compose', 'up')]) == 1


def test_manual_rollback_uses_the_original_schema_and_profile(host):
    before = original_files(host)
    host.run()
    host.restore_saved(host.backup)
    assert host.live_image == OLD and host.timer and all(host.running.values())
    assert original_files(host) == before
    assert_original_data(host)


def test_repeated_install_on_the_same_digest_does_not_restart_containers(host):
    host.run()
    stop_count = len([call for call in host.calls if call[:2] == ('docker', 'stop')])
    host.run()
    assert len([call for call in host.calls if call[:2] == ('docker', 'stop')]) == stop_count
    assert host.live_image == NEW and host.timer and all(host.running.values())


def test_originally_inactive_watchdog_timer_is_not_enabled(host):
    host.timer = False
    host.run()
    assert not host.timer
    assert all(host.running.values())


def test_rollback_never_overwrites_data_of_an_engine_that_failed_to_stop(host):
    host.fault = 'rollback_stop'
    with pytest.raises(update.UpdateError, match='ikke bekræftet'):
        host.run()
    assert host.live_image == NEW and host.running[update.OPENWA]
    assert not host.running[update.PAGER] and not host.timer
    assert (host.profile / 'credential').read_text() == 'new-profile'
    assert (host.data / 'new-version-only').exists()
    assert host.state['archive_sha256'] == update.validate_archive(host.backup / 'openwa-data.tar', SESSION)


def test_invalid_backup_is_rejected_before_manual_rollback_stops_services(host):
    host.run()
    (host.backup / 'openwa-data.tar').write_bytes(b'broken archive')
    calls_before = len(host.calls)
    with pytest.raises(tarfile.ReadError):
        host.restore_saved(host.backup)
    assert host.live_image == NEW and host.timer and all(host.running.values())
    assert not host.calls[calls_before:]


def test_env_update_removes_duplicate_and_exported_pins_without_touching_secrets():
    source = 'TOKEN="a#b$literal"\nSMS_WHATSAPP_OPENWA_IMAGE=old\nexport SMS_WHATSAPP_OPENWA_IMAGE=other\n'
    result = update.updated_env(source, 'pinned')
    assert result.startswith('TOKEN="a#b$literal"\n')
    assert result.count('SMS_WHATSAPP_OPENWA_IMAGE=') == 1
    assert 'SMS_WHATSAPP_OPENWA_IMAGE=pinned\n' in result


def test_compose_patch_changes_only_primary_service_and_is_idempotent():
    original = (ROOT / 'compose/sms-whatsapp/compose.yml').read_text()
    patched = update.updated_compose(original)
    assert patched == original
    assert update.updated_compose(patched) == patched
