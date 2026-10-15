"""Real Git preservation tests; Docker/updater are isolated local fakes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/apply-reviewed-pager.sh'


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def setup(tmp_path, wrong_volume=False, dirty=True, failed_update=False, driver='cudy', wrong_sha=False, conflict=False):
    remote, app, commands = (tmp_path / name for name in ('remote', 'app', 'commands'))
    remote.mkdir(); commands.mkdir()
    git(remote, 'init', '-q', '-b', 'main')
    git(remote, 'config', 'user.name', 'Test')
    git(remote, 'config', 'user.email', 'test@example.invalid')
    (remote / '.gitignore').write_text('.env\n')
    (remote / 'reader.py').write_text('original\n')
    git(remote, 'add', '.')
    git(remote, 'commit', '-qm', 'initial host')
    original_sha = git(remote, 'rev-parse', 'HEAD')
    subprocess.run(['git', 'clone', '-q', str(remote), str(app)], check=True)
    git(app, 'config', 'user.name', 'Test'); git(app, 'config', 'user.email', 'test@example.invalid')
    git(remote, 'switch', '-qc', 'codex/pager-reliability-review')
    (remote / 'scripts').mkdir()
    updater = remote / 'scripts/update-sbr-pager.sh'
    updater.write_text('#!/bin/sh\nprintf installed > update-ran\nexit ' + ('1' if failed_update else '0') + '\n')
    (remote / 'reader.py').write_text('reviewed\n')
    if conflict:
        (remote / 'local.txt').write_text('tracked in review\n')
    git(remote, 'add', '.')
    git(remote, 'commit', '-qm', 'review')
    sha = git(remote, 'rev-parse', 'HEAD')
    if dirty:
        (app / 'reader.py').write_text('staged local fix\n')
        git(app, 'add', 'reader.py')
        (app / 'reader.py').write_text('unstaged local fix\n')
    (app / '.env').write_text('SMS_MODEM_DRIVER=' + driver + '\nCUDY_PASSWORD=synthetic-secret\n')
    (app / 'local.txt').write_text('untracked user data\n')
    snapshots = {}
    for name, project, service, volume in [
        ('sbr-sms-whatsapp', 'sms-whatsapp', 'sms-whatsapp', 'sms-whatsapp_sms_whatsapp_data'),
        ('sbr-pager-sms-gateway', 'sms-gateway', 'sms-gateway', 'sms-gateway_sms_gateway_data'),
        ('racher-sms-openwa', 'sms-whatsapp', 'openwa', None),
    ]:
        snapshots[name] = [{'Config': {'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': service}},
                            'Mounts': [{'Type': 'volume', 'Name': 'unexpected' if wrong_volume else volume, 'Destination': '/data'}] if volume else []}]
    (commands / 'docker').write_text('#!' + sys.executable + '\nimport json,sys\ndata=' + repr(snapshots) + '\nassert sys.argv[1]=="inspect"\nprint(json.dumps(data[sys.argv[2]]))\n')
    (commands / 'docker').chmod(0o755)
    env = dict(os.environ, SBR_PAGER_ROOT=str(app), PATH=str(commands) + os.pathsep + os.environ['PATH'])
    result = subprocess.run(['bash', str(SCRIPT), '0' * 40 if wrong_sha else sha], env=env, capture_output=True, text=True, timeout=10)
    return app, result, original_sha, sha


@pytest.mark.parametrize('dirty', [True, False])
def test_success_keeps_original_git_history_env_and_untracked_data(tmp_path, dirty):
    app, result, original_sha, sha = setup(tmp_path, dirty=dirty)
    assert result.returncode == 0, result.stderr
    assert git(app, 'rev-parse', 'HEAD') == sha
    assert (app / 'reader.py').read_text() == 'reviewed\n'
    assert (app / 'local.txt').read_text() == 'untracked user data\n'
    assert 'synthetic-secret' not in result.stdout + result.stderr
    backup = next((app / '.git/pager-review-backups').iterdir())
    assert (backup / 'env.backup').read_text() == (app / '.env').read_text()
    assert (backup / 'env.backup').stat().st_mode & 0o077 == 0
    assert backup.stat().st_mode & 0o077 == 0
    assert (backup / 'old-head.txt').read_text().strip() == original_sha
    with tarfile.open(backup / 'committed-source.tar.gz') as source:
        assert source.extractfile('reader.py').read() == b'original\n'
    if dirty:
        stash = (backup / 'stash.txt').read_text().strip()
        assert git(app, 'rev-parse', 'refs/stash') == stash
        assert git(app, 'show', stash + ':reader.py') == 'unstaged local fix'
        assert git(app, 'show', stash + '^2:reader.py') == 'staged local fix'


def test_failed_updater_restores_original_branch_and_staged_working_changes(tmp_path):
    app, result, original_sha, sha = setup(tmp_path, failed_update=True)
    assert result.returncode == 1
    assert git(app, 'rev-parse', 'HEAD') == original_sha
    assert git(app, 'branch', '--show-current') == 'main'
    assert (app / 'reader.py').read_text() == 'unstaged local fix\n'
    assert git(app, 'show', ':reader.py') == 'staged local fix'
    assert (app / 'local.txt').read_text() == 'untracked user data\n'
    assert git(app, 'rev-parse', 'refs/stash')


@pytest.mark.parametrize('options', [{'wrong_volume': True}, {'wrong_sha': True}, {'driver': 'usb'}])
def test_unverified_topology_or_commit_stops_before_touching_working_code(tmp_path, options):
    app, result, original_sha, sha = setup(tmp_path, **options)
    assert result.returncode != 0
    assert git(app, 'rev-parse', 'HEAD') == original_sha
    assert (app / 'reader.py').read_text() == 'unstaged local fix\n'
    assert git(app, 'show', ':reader.py') == 'staged local fix'
    assert not (app / 'update-ran').exists()
    assert not (app / '.git/pager-review-backups').exists()


def test_untracked_file_conflict_stops_and_restores_stashed_local_changes(tmp_path):
    app, result, original_sha, sha = setup(tmp_path, conflict=True)
    assert result.returncode != 0
    assert git(app, 'rev-parse', 'HEAD') == original_sha
    assert (app / 'reader.py').read_text() == 'unstaged local fix\n'
    assert git(app, 'show', ':reader.py') == 'staged local fix'
    assert (app / 'local.txt').read_text() == 'untracked user data\n'
    assert not (app / 'update-ran').exists()
