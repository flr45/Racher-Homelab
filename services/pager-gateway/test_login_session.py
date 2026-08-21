from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class LoginSessionTests(unittest.TestCase):
    def test_setup_login_and_authenticated_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import os
                os.environ['PAGER_COOKIE_SECURE'] = '0'
                import app

                app.app.config.update(TESTING=True)
                client = app.app.test_client()

                setup = client.get('/setup')
                assert setup.status_code == 200, setup.status_code
                with client.session_transaction() as sess:
                    csrf = sess['csrf_token']

                created = client.post('/setup', data={
                    'csrf_token': csrf,
                    'display_name': 'Admin',
                    'username': 'admin',
                    'password': 'meget-hemmelig-admin',
                })
                assert created.status_code == 302, created.get_data(as_text=True)

                me = client.get('/api/me')
                assert me.status_code == 200, me.get_data(as_text=True)
                assert me.get_json()['username'] == 'admin'

                with client.session_transaction() as sess:
                    csrf = sess['csrf_token']
                logged_out = client.post('/logout', data={'csrf_token': csrf})
                assert logged_out.status_code == 302, logged_out.status_code

                login_page = client.get('/login')
                assert login_page.status_code == 200, login_page.status_code
                with client.session_transaction() as sess:
                    csrf = sess['csrf_token']
                logged_in = client.post('/login', data={
                    'csrf_token': csrf,
                    'username': 'admin',
                    'password': 'meget-hemmelig-admin',
                })
                assert logged_in.status_code == 302, logged_in.get_data(as_text=True)

                status = client.get('/api/status')
                assert status.status_code == 200, status.get_data(as_text=True)

                app.source.stop()
                """
            )
            env = os.environ.copy()
            env['PAGER_DATA_DIR'] = tmp
            env['PAGER_DB_PATH'] = str(Path(tmp) / 'pager.db')
            result = subprocess.run(
                [sys.executable, '-c', script],
                cwd=Path(__file__).resolve().parent,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
