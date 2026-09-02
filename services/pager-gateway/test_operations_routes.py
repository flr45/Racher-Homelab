from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class OperationsRoutesTests(unittest.TestCase):
    def test_system_test_and_status_do_not_create_alarm_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import wsgi
                import app_core as core

                app = wsgi.app
                app.config.update(TESTING=True)
                client = app.test_client()

                client.get('/setup')
                with client.session_transaction() as sess:
                    csrf = sess['csrf_token']
                created = client.post('/setup', data={
                    'csrf_token': csrf,
                    'display_name': 'Admin',
                    'username': 'admin',
                    'password': 'meget-hemmelig-admin',
                })
                assert created.status_code == 302, created.status_code

                before = core.storage.message_count()
                with client.session_transaction() as sess:
                    csrf = sess['csrf_token']
                tested = client.post(
                    '/api/system/test-delivery',
                    json={}, headers={'X-CSRF-Token': csrf},
                )
                assert tested.status_code == 200, tested.get_data(as_text=True)
                payload = tested.get_json()
                assert payload['ok'] is True, payload
                assert payload['checks']['database']['status'] == 'ok'
                assert payload['checks']['routing']['status'] == 'ok'
                assert payload['checks']['pushover']['status'] == 'disabled'
                assert payload['checks']['web_push']['status'] == 'disabled'
                assert core.storage.message_count() == before

                status = client.get('/api/status')
                assert status.status_code == 200, status.get_data(as_text=True)
                status_payload = status.get_json()
                assert status_payload['alarm_window_minutes'] == 120
                assert 'hour' in status_payload['quality']
                assert 'day' in status_payload['quality']

                feed = client.get('/api/messages?scope=feed&limit=20')
                assert feed.status_code == 200
                assert feed.get_json() == []
                core.source.stop()
                """
            )
            env = os.environ.copy()
            env['PAGER_DATA_DIR'] = tmp
            env['PAGER_DB_PATH'] = str(Path(tmp) / 'pager.db')
            env['PAGER_COOKIE_SECURE'] = '0'
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
