from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class ExternalMonitorConfigTests(unittest.TestCase):
    def test_config_is_admin_managed_and_tailscale_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import os
                os.environ['PAGER_COOKIE_SECURE'] = '0'
                import app

                app.app.config.update(TESTING=True)
                client = app.app.test_client()
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
                with client.session_transaction() as sess:
                    csrf = sess['csrf_token']

                saved = client.post('/api/settings', json={
                    'external_monitor_enabled': True,
                    'external_monitor_sms_to': '12 34 56 78',
                    'external_monitor_failure_threshold': 3,
                }, headers={'X-CSRF-Token': csrf})
                assert saved.status_code == 200, saved.get_data(as_text=True)

                settings = client.get('/api/settings').get_json()
                assert settings['external_monitor_enabled'] == '1'
                assert settings['external_monitor_sms_to'] == '+4512345678'
                assert settings['external_monitor_failure_threshold'] == '3'

                lan = client.get(
                    '/api/external-monitor/config',
                    environ_base={'REMOTE_ADDR': '192.168.1.10'},
                )
                assert lan.status_code == 403, lan.get_data(as_text=True)

                tailscale = client.get(
                    '/api/external-monitor/config',
                    environ_base={'REMOTE_ADDR': '100.111.28.12'},
                )
                assert tailscale.status_code == 200, tailscale.get_data(as_text=True)
                config = tailscale.get_json()
                assert config['enabled'] is True
                assert config['sms_to'] == '+4512345678'
                assert config['failure_threshold'] == 3

                bad = client.post('/api/settings', json={
                    'external_monitor_enabled': True,
                    'external_monitor_sms_to': 'abc',
                    'external_monitor_failure_threshold': 3,
                }, headers={'X-CSRF-Token': csrf})
                assert bad.status_code == 400, bad.get_data(as_text=True)

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
