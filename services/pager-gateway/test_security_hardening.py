from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class SecurityHardeningTests(unittest.TestCase):
    def test_login_throttle_secure_cookie_and_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import time
                from collections import deque

                import app

                app.app.config.update(TESTING=True)
                base_url = 'https://pager.test'
                assert app.app.config['MAX_CONTENT_LENGTH'] == 10 * 1024 * 1024

                setup_client = app.app.test_client()
                setup_page = setup_client.get('/setup', base_url=base_url, headers={'X-Forwarded-Proto': 'https'})
                assert setup_page.status_code == 200
                assert setup_page.headers['Cache-Control'] == 'no-store'
                with setup_client.session_transaction(base_url=base_url) as sess:
                    csrf = sess['csrf_token']
                created = setup_client.post('/setup', base_url=base_url, data={
                    'csrf_token': csrf,
                    'display_name': 'Admin',
                    'username': 'admin',
                    'password': 'meget-hemmelig-admin',
                })
                assert created.status_code == 302, created.status_code

                client = app.app.test_client()
                login_page = client.get('/login', base_url=base_url, headers={'X-Forwarded-Proto': 'https'})
                assert login_page.status_code == 200
                assert login_page.headers['X-Content-Type-Options'] == 'nosniff'
                assert login_page.headers['X-Frame-Options'] == 'DENY'
                assert login_page.headers['Referrer-Policy'] == 'no-referrer'
                assert login_page.headers['Cross-Origin-Opener-Policy'] == 'same-origin'
                assert login_page.headers['X-Permitted-Cross-Domain-Policies'] == 'none'
                csp = login_page.headers['Content-Security-Policy']
                assert "default-src 'self'" in csp
                assert "script-src 'self'" in csp
                assert "style-src 'self'" in csp
                assert "connect-src 'self'" in csp
                assert "worker-src 'self'" in csp
                assert "form-action 'self'" in csp
                assert "frame-ancestors 'none'" in csp
                assert login_page.headers['Strict-Transport-Security'].startswith('max-age=')
                assert login_page.headers['Cache-Control'] == 'no-store'
                assert 'Secure' in login_page.headers.get('Set-Cookie', '')

                with client.session_transaction(base_url=base_url) as sess:
                    csrf = sess['csrf_token']
                headers = {'CF-Connecting-IP': '203.0.113.10'}
                for attempt in range(5):
                    failed = client.post('/login', base_url=base_url, data={
                        'csrf_token': csrf,
                        'username': 'admin',
                        'password': 'forkert-password',
                    }, headers=headers)
                    assert failed.status_code == 200, (attempt, failed.status_code)

                blocked = client.post('/login', base_url=base_url, data={
                    'csrf_token': csrf,
                    'username': 'admin',
                    'password': 'meget-hemmelig-admin',
                }, headers=headers)
                assert blocked.status_code == 429, blocked.status_code
                assert int(blocked.headers['Retry-After']) > 0

                # A single hostile client must not trivially lock the account for
                # everybody. A different source can still authenticate because
                # the username-wide bucket has a deliberately higher threshold.
                other = app.app.test_client()
                other.get('/login', base_url=base_url)
                with other.session_transaction(base_url=base_url) as sess:
                    other_csrf = sess['csrf_token']
                success = other.post('/login', base_url=base_url, data={
                    'csrf_token': other_csrf,
                    'username': 'admin',
                    'password': 'meget-hemmelig-admin',
                }, headers={'CF-Connecting-IP': '203.0.113.11'})
                assert success.status_code == 302, success.status_code
                assert success.headers['Location'].endswith('/')

                home = other.get('/', base_url=base_url)
                assert home.status_code == 200
                assert home.headers['Cache-Control'] == 'no-store'

                api = other.get('/api/status', base_url=base_url)
                assert api.status_code == 200, api.get_data(as_text=True)
                assert api.headers['Cache-Control'] == 'no-store'

                # Randomized usernames/IPs must not create unbounded in-memory
                # throttle bookkeeping on the small appliance.
                now = time.monotonic()
                with app._login_lock:
                    app._login_failures.clear()
                    app._login_blocked_until.clear()
                    for index in range(app._LOGIN_STATE_MAX_KEYS + 50):
                        app._login_failures[f'noise:{index}'] = deque([now])
                    app._trim_login_state_locked(now, force=True)
                    assert len(app._login_failures) <= app._LOGIN_STATE_MAX_KEYS

                app.source.stop()
                """
            )
            env = os.environ.copy()
            env['PAGER_DATA_DIR'] = tmp
            env['PAGER_DB_PATH'] = str(Path(tmp) / 'pager.db')
            env['PAGER_COOKIE_SECURE'] = '1'
            result = subprocess.run(
                [sys.executable, '-c', script],
                cwd=Path(__file__).resolve().parent,
                env=env,
                text=True,
                capture_output=True,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
