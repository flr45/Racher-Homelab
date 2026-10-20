"""Opt-in real-image checks. Network is disabled for every running container."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(os.getenv('SBR_TEST_OPENWA_DOCKER') != '1', reason='Opt-in real OpenWA image gate')
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('openwa_update_docker', ROOT / 'scripts/update-openwa.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, capture_output=True, timeout=120, **kwargs).stdout


def test_real_image_roundtrip_restores_sqlite_hidden_key_and_browser_profile(tmp_path):
    volume = 'openwa-update-test-' + uuid.uuid4().hex
    seed = tmp_path / 'seed'
    profile = seed / 'sessions/session-existing'
    profile.mkdir(parents=True)
    (profile / 'credential').write_text('synthetic-saved-login')
    (seed / '.api-key').write_text('synthetic-api-key')
    with sqlite3.connect(seed / 'main.sqlite') as db:
        db.execute('CREATE TABLE old_schema (value TEXT)')
        db.execute("INSERT INTO old_schema VALUES ('preserved')")
    docker('volume', 'create', volume)
    try:
        mount = f'type=volume,source={volume},target=/restore'
        docker('run', '--rm', '--network', 'none', '--read-only', '--user', '0',
               '--mount', mount, '--mount', f'type=bind,source={seed},target=/seed,readonly',
               '--entrypoint', 'node', update.IMAGE, '-e',
               "require('fs').cpSync('/seed', '/restore', {recursive: true})")
        updater = update.Updater(tmp_path)
        updater.state = {'volume': volume, 'rollback_image': update.IMAGE}
        archive = tmp_path / 'backup.tar'
        updater.archive_volume(archive)
        assert len(update.validate_archive(archive, 'existing')) == 64
        docker('run', '--rm', '--network', 'none', '--user', '0', '--mount', mount,
               '--entrypoint', 'node', update.IMAGE, '-e',
               "const f=require('fs'); f.writeFileSync('/restore/main.sqlite','new-schema');"
               "f.writeFileSync('/restore/new-only','new');"
               "f.writeFileSync('/restore/sessions/session-existing/credential','changed')")
        with archive.open('rb') as source:
            updater.command('docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                            '--user', '0', '--mount', mount, '--entrypoint', 'node', update.IMAGE,
                            '-e', update.RESTORE, input_file=source)
        result = json.loads(docker('run', '--rm', '--network', 'none', '--read-only', '--user', '0',
                                  '--mount', mount, '--entrypoint', 'node', update.IMAGE, '-e',
                                  "const f=require('fs'); console.log(JSON.stringify({"
                                  "db:f.readFileSync('/restore/main.sqlite').toString('base64'),"
                                  "key:f.readFileSync('/restore/.api-key','utf8'),"
                                  "credential:f.readFileSync('/restore/sessions/session-existing/credential','utf8'),"
                                  "extra:f.existsSync('/restore/new-only')}))"))
        assert result['key'] == 'synthetic-api-key'
        assert result['credential'] == 'synthetic-saved-login'
        assert result['extra'] is False
        restored = tmp_path / 'restored.sqlite'
        restored.write_bytes(base64.b64decode(result['db']))
        with sqlite3.connect(restored) as db:
            assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert db.execute('SELECT value FROM old_schema').fetchone() == ('preserved',)
    finally:
        docker('volume', 'rm', volume)


def test_real_image_boots_and_accepts_pager_api_header_without_whatsapp_access():
    suffix = uuid.uuid4().hex
    volume, container = 'openwa-boot-test-' + suffix, 'openwa-boot-test-' + suffix
    docker('volume', 'create', volume)
    try:
        docker('run', '-d', '--name', container, '--network', 'none', '--init',
               '--memory', '1200m', '--pids-limit', '1024', '--tmpfs', '/tmp',
               '--security-opt', 'no-new-privileges:true', '--mount', f'type=volume,source={volume},target=/app/data',
               '-e', 'NODE_ENV=production', '-e', 'PORT=2785', '-e', 'AUTO_START_SESSIONS=false',
               '-e', 'CSP_UPGRADE_INSECURE_REQUESTS=false', update.IMAGE)
        probe = (
            "const f=require('fs'); (async()=>{const k=f.readFileSync('/app/data/.api-key','utf8').trim();"
            "const r=await fetch('http://127.0.0.1:2785/api/sessions',{headers:{'X-API-Key':k},"
            "signal:AbortSignal.timeout(3000)});process.exit(r.status===200?0:1)})().catch(()=>process.exit(1))"
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = subprocess.run(['docker', 'exec', container, 'node', '-e', probe],
                                    capture_output=True, timeout=8)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail('Pinned OpenWA image did not boot and authenticate within 90 seconds')
        docker('stop', '--time', '75', container)
    finally:
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=90)
        docker('volume', 'rm', volume)
