#!/usr/bin/env python3
"""Pinned OpenWA update for the inspected racherserver topology.

Run on the host, independently of its checked-out Pager commit. Nothing here
logs out a session, releases held deliveries, or sends a test message.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time


IMAGE = "ghcr.io/rmyndharis/openwa:0.25.0"
IMAGE_SETTING = '${SMS_WHATSAPP_OPENWA_IMAGE:-' + IMAGE + '}'
OPENWA = "racher-sms-openwa"
PAGER = "sbr-sms-whatsapp"
TIMER = "sbr-pager-watchdog.timer"
SERVICE = "sbr-pager-watchdog.service"

PROBE = r"""
const fs = require('fs');
(async () => {
  const {key, session} = JSON.parse(fs.readFileSync(0, 'utf8'));
  const response = await fetch('http://127.0.0.1:2785/api/sessions/' +
    encodeURIComponent(session), {
      headers: {'X-API-Key': key}, signal: AbortSignal.timeout(5000)
    });
  if (!response.ok) { console.log(JSON.stringify({http: response.status})); return; }
  const data = await response.json();
  console.log(JSON.stringify({status: data.status, engineLoaded: data.engineLoaded}));
})().catch(() => { console.log(JSON.stringify({status: 'api_unavailable'})); });
"""

RESTORE = r"""
const fs = require('fs'), cp = require('child_process');
// The caller validated this exact named volume and a complete archive first.
for (const name of fs.readdirSync('/restore')) {
  fs.rmSync('/restore/' + name, {recursive: true, force: true});
}
const result = cp.spawnSync('tar', ['--no-same-owner', '-xpf', '-', '-C', '/restore'],
  {stdio: 'inherit'});
process.exit(result.status === null ? 1 : result.status);
"""


class UpdateError(RuntimeError):
    pass


def private_write(path, data):
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)


def atomic_write(path, content, mode):
    staged = path.with_name(path.name + f'.openwa-{os.getpid()}')
    try:
        private_write(staged, content)
        staged.chmod(mode)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def updated_compose(text):
    """Change only the existing primary service, including older host checkouts."""
    match = re.search(r'(?m)^  openwa:[ \t]*\n(?P<body>(?:[ \t].*\n|\n)*?)(?=^  [a-zA-Z0-9_-]+:|^\S|\Z)', text)
    if not match:
        raise UpdateError('Den forventede OpenWA-service mangler i Compose-filen.')
    body = match['body']
    image_line = re.findall(r'(?m)^    image: (.+)$', body)
    if len(image_line) != 1 or not (
        re.fullmatch(r'ghcr\.io/rmyndharis/openwa:[A-Za-z0-9_.-]+(?:@sha256:[0-9a-f]{64})?', image_line[0])
        or image_line[0] == IMAGE_SETTING
    ):
        raise UpdateError('OpenWA-image afviger; filen ændres ikke automatisk.')
    body = re.sub(r'(?m)^    image: .+$', '    image: ' + IMAGE_SETTING, body)
    if re.search(r'(?m)^    stop_grace_period:', body):
        body = re.sub(r'(?m)^    stop_grace_period:.*$', '    stop_grace_period: 75s', body)
    else:
        body = body.replace('    image: ' + IMAGE_SETTING + '\n',
                            '    image: ' + IMAGE_SETTING + '\n    stop_grace_period: 75s\n', 1)
    if not re.search(r'(?m)^      AUTO_START_SESSIONS:', body):
        raise UpdateError('Den forventede AUTO_START_SESSIONS-indstilling mangler.')
    # Explicit host env wins over the default; the update writes it as true.
    body = re.sub(r'(?m)^      AUTO_START_SESSIONS:.*$',
                  '      AUTO_START_SESSIONS: ${SMS_WHATSAPP_OPENWA_AUTO_START_SESSIONS:-false}', body)
    return text[:match.start('body')] + body + text[match.end('body'):]


def updated_env(text, image):
    settings = {'SMS_WHATSAPP_OPENWA_IMAGE': image,
                'SMS_WHATSAPP_OPENWA_AUTO_START_SESSIONS': 'true'}
    lines = [line for line in text.splitlines()
             if not re.match(r'^\s*(?:export\s+)?(?:' + '|'.join(settings) + r')\s*=', line)]
    return '\n'.join(lines) + '\n' + ''.join(f'{key}={value}\n' for key, value in settings.items())


def validate_archive(path, session):
    with tarfile.open(path, 'r:') as archive:
        names = []
        for member in archive:
            name = member.name.removeprefix('./')
            if name.startswith('/') or '..' in Path(name).parts:
                raise UpdateError('Backup-arkivet har en uventet sti.')
            names.append(name)
        if 'main.sqlite' not in names or not any(
            name.startswith(f'sessions/session-{session}/') for name in names
        ):
            raise UpdateError('Backup mangler main.sqlite eller den eksisterende WhatsApp-profil.')
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


class Updater:
    def __init__(self, root, ready_seconds=240):
        self.root = root.resolve()
        self.env_file = self.root / '.env'
        self.compose_file = self.root / 'compose/sms-whatsapp/compose.yml'
        self.ready_seconds = ready_seconds
        self.backup = None
        self.state = {}
        self.timer_paused = False
        self.pager_stopped = False
        self.openwa_stopped = False
        self.files_changed = False
        self.replacement_started = False
        self.recovery_failed = False
        self.sudo = [] if os.geteuid() == 0 else ['sudo']

    def command(self, *args, input=None, input_file=None, output=None, check=True,
                timeout=600, merge_stderr=False):
        kwargs = {'stdin': input_file} if input_file else {'input': input}
        result = subprocess.run(args, **kwargs, stdout=output or subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                                timeout=timeout, cwd=self.root)
        if check and result.returncode:
            if self.backup:
                with (self.backup / 'command-errors.log').open('ab') as log:
                    log.write((' '.join(args[:3]) + '\n').encode() + (result.stderr or b'') + b'\n')
            raise UpdateError(f'Kommando mislykkedes: {" ".join(args[:3])}. Se den private backupmappe.')
        return result

    def inspect(self, name):
        return json.loads(self.command('docker', 'inspect', name).stdout)[0]

    def save_state(self):
        private_write(self.backup / 'state.json', json.dumps(self.state, indent=2))

    def compose(self, *args, override=None):
        # Pin project/paths; validate resolved image/mount/env before replacement.
        return self.command('docker', 'compose', '--project-name', 'sms-whatsapp',
                            '--env-file', str(self.env_file), '-f', str(self.compose_file),
                            *(['-f', str(override)] if override else []), *args)

    def archive_volume(self, path):
        # The existing image supplies tar without starting the application. Stream
        # directly to disk: browser profiles can be much larger than host RAM.
        self.assert_volume_stopped()
        with path.open('wb') as target:
            self.command('docker', 'run', '--rm', '--network', 'none', '--read-only',
                         '--user', '0', '--security-opt', 'no-new-privileges:true',
                         '--mount', f'type=volume,source={self.state["volume"]},target=/snapshot,readonly',
                         '--entrypoint', 'tar', self.state['rollback_image'],
                         '-C', '/snapshot', '-cpf', '-', '.', output=target)

    def assert_volume_stopped(self):
        users = self.command('docker', 'ps', '--no-trunc', '-q', '--filter',
                             f'volume={self.state["volume"]}').stdout.strip()
        if users:
            raise UpdateError('En kørende container bruger OpenWA-data. Backup/gendannelse er stoppet.')

    def probe(self):
        result = self.command('docker', 'exec', '-i', OPENWA, 'node', '-e', PROBE,
                              input=json.dumps(self.credentials).encode(), check=False, timeout=12)
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            return {'status': 'api_unavailable'}

    def pause(self):
        # Validate sudo before changing either the timer or a container.
        if self.sudo:
            self.command('sudo', '-v', timeout=120)
        self.state['timer_was_active'] = self.command(
            'systemctl', 'is-active', '--quiet', TIMER, check=False).returncode == 0
        self.save_state()
        if self.state['timer_was_active']:
            self.timer_paused = True
            self.command(*self.sudo, 'systemctl', 'stop', TIMER)
        # A running Type=oneshot service can still be "activating", for which
        # is-active is false. Stop it unconditionally before touching containers.
        self.command(*self.sudo, 'systemctl', 'stop', SERVICE)
        self.pager_stopped = True
        self.command('docker', 'stop', '--time', '75', PAGER)
        self.openwa_stopped = True
        self.command('docker', 'stop', '--time', '75', OPENWA)

    def resume(self):
        if self.pager_stopped:
            self.command('docker', 'start', PAGER)
            self.pager_stopped = False
        if self.timer_paused:
            self.command(*self.sudo, 'systemctl', 'start', TIMER)
            self.timer_paused = False

    def preflight(self):
        for path in (self.env_file, self.compose_file):
            if not path.is_file() or path.is_symlink():
                raise UpdateError(f'Mangler en almindelig fil: {path}')
        self.old_env = self.env_file.read_bytes()
        self.old_compose = self.compose_file.read_bytes()
        self.compose_mode = self.compose_file.stat().st_mode & 0o777
        self.new_compose = updated_compose(self.old_compose.decode())
        current, pager = self.inspect(OPENWA), self.inspect(PAGER)
        for item, service in ((current, 'openwa'), (pager, 'sms-whatsapp')):
            labels = item['Config'].get('Labels') or {}
            if labels.get('com.docker.compose.project') != 'sms-whatsapp' or labels.get(
                'com.docker.compose.service') != service or not item['State']['Running']:
                raise UpdateError('Containere eller Compose-projekt afviger fra den gennemgåede opsætning.')
            files = labels.get('com.docker.compose.project.config_files', '').split(',')
            if str(self.compose_file) not in files or any(
                file not in {str(self.compose_file), str(self.root / 'compose/sms-whatsapp/offsite-backup.yml')}
                for file in files
            ):
                raise UpdateError('Aktive Compose-filer afviger fra den gennemgåede opsætning.')
        mounts = [mount for mount in current['Mounts'] if mount['Destination'] == '/app/data']
        if len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get(
            'Name') != 'sms-whatsapp_sms_whatsapp_openwa_data':
            raise UpdateError('Stop: OpenWA-datavolumen afviger; ingen ændringer foretaget.')
        volume = mounts[0]['Name']
        users = self.command('docker', 'ps', '--no-trunc', '-q', '--filter', f'volume={volume}').stdout.decode().split()
        if users != [current['Id']]:
            raise UpdateError('Flere kørende containere deler OpenWA-data; opdateringen stopper.')
        env = dict(item.split('=', 1) for item in pager['Config']['Env'] if '=' in item)
        self.credentials = {'key': env.get('OPENWA_API_KEY', '').strip(),
                            'session': env.get('OPENWA_SESSION_ID', '').strip()}
        if not self.credentials['key'] or not re.fullmatch(r'[A-Za-z0-9_-]+', self.credentials['session'] or ''):
            raise UpdateError('Pager mangler en brugbar OpenWA-nøgle eller sessions-id.')
        before = self.probe()
        if before.get('status') != 'ready' or before.get('engineLoaded') is False:
            raise UpdateError('WhatsApp skal være ready før opdateringen. Tilknyt den eksisterende session først.')
        git_dir = Path(self.command('git', 'rev-parse', '--absolute-git-dir').stdout.decode().strip())
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + f'-{os.getpid()}-{time.time_ns()}'
        self.backup = git_dir / 'openwa-update-backups' / stamp
        self.backup.mkdir(parents=True, mode=0o700)
        self.backup.chmod(0o700)
        private_write(self.backup / 'env.before', self.old_env)
        private_write(self.backup / 'compose.before.yml', self.old_compose)
        private_write(self.backup / 'container.before.json', json.dumps(current))
        shutil.copyfile(Path(__file__), self.backup / 'update-openwa.py')
        (self.backup / 'update-openwa.py').chmod(0o600)
        self.state = {'root': str(self.root), 'volume': volume, 'old_image': current['Image'],
                      'rollback_image': 'sbr-openwa-rollback:' + stamp, 'session': self.credentials['session'],
                      'compose_mode': self.compose_mode, 'stage': 'preflight', 'version': '0.25.0'}
        self.save_state()
        self.compose('config', '--quiet')
        # Pull and verify utility availability before the short maintenance window.
        print('Henter OpenWA 0.25.0. De eksisterende containere kører imens.', flush=True)
        self.command('docker', 'pull', IMAGE)
        target = json.loads(self.command('docker', 'image', 'inspect', IMAGE).stdout)[0]
        digests = [digest for digest in target['RepoDigests'] if digest.startswith('ghcr.io/rmyndharis/openwa@sha256:')]
        if not digests:
            raise UpdateError('Det hentede image mangler et fast repository-digest.')
        self.state['target_image'] = IMAGE + '@' + digests[0].split('@', 1)[1]
        self.state['target_id'] = target['Id']
        self.command('docker', 'tag', current['Image'], self.state['rollback_image'])
        self.command('docker', 'run', '--rm', '--network', 'none', '--read-only',
                     '--entrypoint', 'tar', current['Image'], '--version')
        self.save_state()
        latest = self.inspect(OPENWA)
        if latest['Image'] != current['Image'] or self.probe().get('status') != 'ready':
            raise UpdateError('OpenWA ændrede image eller login under download; driften ændres ikke.')

        self.already_current = (current['Image'] == target['Id']
                                and self.new_compose.encode() == self.old_compose
                                and updated_env(self.old_env.decode(), self.state['target_image']).encode() == self.old_env)

    def rollback(self):
        print('Gendanner tidligere OpenWA-image og fulde data fra backup.', flush=True)
        self.command('docker', 'stop', '--time', '75', OPENWA, check=False)
        archive = self.backup / 'openwa-data.tar'
        if validate_archive(archive, self.state['session']) != self.state['archive_sha256']:
            raise UpdateError('Backupens checksum afviger; automatisk gendannelse stoppet.')
        # Retain the failed/new schema and profile before replacing their files.
        self.archive_volume(self.backup / ('openwa-before-rollback-' + str(time.time_ns()) + '.tar'))
        self.assert_volume_stopped()
        # Migration ledgers and Chromium profiles must match the old image too.
        with archive.open('rb') as source:
            self.command('docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                         '--user', '0', '--security-opt', 'no-new-privileges:true',
                         '--mount', f'type=volume,source={self.state["volume"]},target=/restore',
                         '--entrypoint', 'node', self.state['rollback_image'], '-e', RESTORE,
                         input_file=source)
        atomic_write(self.env_file, self.old_env, 0o600)
        atomic_write(self.compose_file, self.old_compose, self.compose_mode)
        override = self.backup / 'rollback.yml'
        private_write(override, 'services:\n  openwa:\n    image: ' + self.state['rollback_image'] +
                      '\n    environment:\n      AUTO_START_SESSIONS: "true"\n')
        self.compose('up', '-d', '--no-build', '--no-deps', '--pull', 'never', 'openwa', override=override)
        self.openwa_stopped = False
        self.state['stage'] = 'rolled_back'
        self.save_state()

    def update(self):
        self.preflight()
        if self.already_current:
            print('OpenWA 0.25.0 er allerede installeret og fastlåst. Ingen genstart.', flush=True)
            return
        print(f'Privat backupmappe: {self.backup}', flush=True)
        self.pause()
        with (self.backup / 'openwa-before.log').open('wb') as log:
            self.command('docker', 'logs', '--timestamps', OPENWA, output=log, check=False, merge_stderr=True)
        archive = self.backup / 'openwa-data.tar'
        self.archive_volume(archive)
        self.state['archive_sha256'] = validate_archive(archive, self.state['session'])
        self.state['stage'] = 'backed_up'
        self.save_state()
        if self.env_file.read_bytes() != self.old_env or self.compose_file.read_bytes() != self.old_compose:
            raise UpdateError('Opsætningsfilerne blev ændret under forberedelsen; installationen stopper.')
        self.files_changed = True
        atomic_write(self.compose_file, self.new_compose, self.compose_mode)
        atomic_write(self.env_file, updated_env(self.old_env.decode(), self.state['target_image']), 0o600)
        self.compose('config', '--quiet')
        # Validate the resolved mount/image before Compose can replace a container.
        config = json.loads(self.compose('config', '--format', 'json').stdout)
        service = config['services']['openwa']
        data_mounts = [mount for mount in service['volumes'] if mount['target'] == '/app/data']
        source = data_mounts[0]['source'] if len(data_mounts) == 1 else None
        actual = config.get('volumes', {}).get(source, {}).get('name')
        if service['image'] != self.state['target_image'] or actual != self.state['volume'] or str(
            service['environment'].get('AUTO_START_SESSIONS')).lower() != 'true':
            raise UpdateError('Den effektive Compose-konfiguration ændrer image, autostart eller datavolumen uventet.')
        print('Backup er klar. Starter den fastlåste OpenWA-version med det eksisterende login.', flush=True)
        self.replacement_started = True
        self.state['stage'] = 'replacing'
        self.save_state()
        self.compose('up', '-d', '--no-build', '--no-deps', '--pull', 'never', 'openwa')
        installed = self.inspect(OPENWA)
        if installed['Image'] != self.state['target_id'] or not any(
            mount.get('Name') == self.state['volume'] and mount.get('Destination') == '/app/data'
            for mount in installed['Mounts']
        ):
            raise UpdateError('Den nye container bruger ikke det kontrollerede image og datavolumen.')
        deadline = time.monotonic() + self.ready_seconds
        previous = None
        while time.monotonic() < deadline:
            result = self.probe()
            status = result.get('status', 'api_unavailable')
            if status != previous:
                print('OpenWA-status: ' + status, flush=True)
                previous = status
            if status == 'ready' and result.get('engineLoaded') is not False:
                break
            if status in {'qr_ready', 'auth_failure'}:
                print('OpenWA er opdateret, men WhatsApp kræver tilknytning på telefonen.', flush=True)
                break
            time.sleep(5)
        else:
            raise UpdateError('OpenWA blev ikke klar inden for opstartsfristen.')
        self.state['stage'] = 'installed'
        self.state['session_status'] = status
        self.save_state()
        self.openwa_stopped = False
        self.resume()
        print(f'Installeret: {IMAGE}\nBackup og rollback-oplysninger: {self.backup}', flush=True)
        print('Ingen testbesked sendt og ingen tilbageholdte leveringer frigivet.', flush=True)

    def run(self):
        try:
            self.update()
        except (Exception, KeyboardInterrupt):
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                if self.state.get('stage') == 'installed':
                    # The new backend passed. A Pager/timer restart failure must
                    # not undo an already resumed WhatsApp engine and its data.
                    pass
                elif self.replacement_started:
                    self.rollback()
                else:
                    if self.files_changed:
                        atomic_write(self.env_file, self.old_env, 0o600)
                        atomic_write(self.compose_file, self.old_compose, self.compose_mode)
                    if self.openwa_stopped:
                        self.command('docker', 'start', OPENWA)
                        self.openwa_stopped = False
                self.resume()
            except Exception as recovery:
                self.recovery_failed = True
                raise UpdateError(f'Automatisk gendannelse/genstart blev ikke fuldført. Driften er ikke bekræftet. '
                                  f'Backup: {self.backup}. {recovery}') from None
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
            raise

    def restore_saved(self, backup):
        self.backup = backup.resolve()
        self.state = json.loads((self.backup / 'state.json').read_text())
        if self.state['root'] != str(self.root) or self.state['volume'] != 'sms-whatsapp_sms_whatsapp_openwa_data':
            raise UpdateError('Backup passer ikke til projektet og datavolumen.')
        self.old_env = (self.backup / 'env.before').read_bytes()
        self.old_compose = (self.backup / 'compose.before.yml').read_bytes()
        self.compose_mode = self.state['compose_mode']
        if validate_archive(self.backup / 'openwa-data.tar', self.state['session']) != self.state['archive_sha256']:
            raise UpdateError('Backupens checksum afviger; ingen ændringer foretaget.')
        current = self.inspect(OPENWA)
        labels = current['Config'].get('Labels') or {}
        if (labels.get('com.docker.compose.project') != 'sms-whatsapp' or
            labels.get('com.docker.compose.service') != 'openwa' or
            current['Image'] not in {self.state['target_id'], self.state['old_image']}) or not any(
            mount.get('Name') == self.state['volume'] and mount.get('Destination') == '/app/data'
            for mount in current['Mounts']
        ):
            raise UpdateError('Den aktive installation afviger fra denne backup; gendannelsen stopper.')
        # Preserve any operator changes made after the successful upgrade too.
        stamp = str(time.time_ns())
        private_write(self.backup / ('env.before-rollback-' + stamp), self.env_file.read_bytes())
        private_write(self.backup / ('compose.before-rollback-' + stamp + '.yml'), self.compose_file.read_bytes())
        try:
            self.pause()
        except (Exception, KeyboardInterrupt):
            if self.openwa_stopped:
                self.command('docker', 'start', OPENWA)
            self.resume()
            raise
        try:
            self.rollback()
            self.resume()
        except Exception:
            raise UpdateError(f'Gendannelsen/genstart blev ikke fuldført. Driften er ikke bekræftet. Backup: {self.backup}') from None
        print('Tidligere OpenWA-image og fulde data er gendannet. Kontrollér WhatsApp-status.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/SBR-Pager-Gateway'))
    parser.add_argument('--rollback', type=Path, help='Gendan image og fulde data fra denne opdaterings private backupmappe')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error('Projektmappen findes ikke.')
    # Worktrees may have a .git file; resolve the private lock through Git.
    git_dir = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--absolute-git-dir'], text=True).strip()
    lock_path = Path(git_dir) / 'openwa-update.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('En anden OpenWA-opdatering kører allerede.')
        def interrupted(signum, frame):
            raise UpdateError('Opdateringen blev afbrudt; forsøger at genoprette driften.')
        signal.signal(signal.SIGTERM, interrupted)
        try:
            updater = Updater(root)
            if args.rollback:
                updater.restore_saved(args.rollback)
            else:
                updater.run()
        except (Exception, KeyboardInterrupt) as error:
            print(f'STOP: {error}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
