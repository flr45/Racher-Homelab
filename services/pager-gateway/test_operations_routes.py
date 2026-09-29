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
                from datetime import datetime, timedelta, timezone

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

                # Pushover telemetry must reflect every managed destination. The
                # old wrapper reported 1/1 sent even when one or all recipients
                # failed internally inside pushover_destinations.
                core.storage.update_settings({
                    'pushover_enabled': '1',
                    'pushover_app_token': 'app-token',
                })
                wsgi.pushover_destinations.add(
                    'Primær', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
                )
                wsgi.pushover_destinations.add(
                    'Sekundær', 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'
                )

                def fake_pushover_send(_token, user_key, _title, _message):
                    if user_key.startswith('B'):
                        raise RuntimeError('simuleret Pushover-fejl')

                core.pushover.send = fake_pushover_send
                telemetry_id = core.storage.add_message({
                    'received_at': datetime.now(timezone.utc).isoformat(),
                    'protocol': 'POCSAG',
                    'baud': 1200,
                    'station': 'Slagelse',
                    'message': 'BRANDALARM telemetry-test',
                    'raw_line': 'BRANDALARM telemetry-test',
                    'source': 'test',
                    'delivery_eligible': True,
                })
                core.maybe_notify_pushover(telemetry_id, {
                    'received_at': datetime.now(timezone.utc).isoformat(),
                    'protocol': 'POCSAG',
                    'baud': 1200,
                    'station': 'Slagelse',
                    'message': 'BRANDALARM telemetry-test',
                    'raw_line': 'BRANDALARM telemetry-test',
                    'source': 'test',
                    'delivery_eligible': True,
                })
                with wsgi.operations.connect() as conn:
                    delivery = dict(conn.execute(
                        "SELECT * FROM message_delivery WHERE message_id=? AND channel='pushover'",
                        (telemetry_id,),
                    ).fetchone())
                assert delivery['status'] == 'partial', delivery
                assert delivery['target_count'] == 2, delivery
                assert delivery['sent_count'] == 1, delivery
                assert delivery['failed_count'] == 1, delivery
                assert 'Sekundær' in delivery['last_error'], delivery

                # Delivery telemetry must decorate the existing rolling seven-day
                # feed instead of replacing it with Operations' two-hour window.
                six_days_ago = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
                old_alarm_id = core.storage.add_message({
                    'received_at': six_days_ago,
                    'protocol': 'POCSAG',
                    'baud': 1200,
                    'station': 'Slagelse',
                    'message': 'BRANDALARM seks dage gammel',
                    'raw_line': 'BRANDALARM seks dage gammel',
                    'source': 'test',
                    'delivery_eligible': True,
                })
                wsgi.operations.record_delivery(
                    old_alarm_id, 'pushover', 'sent',
                    target_count=1, sent_count=1,
                )

                feed = client.get('/api/messages?scope=feed&limit=20')
                assert feed.status_code == 200
                rows = feed.get_json()
                old_alarm = next((row for row in rows if row['id'] == old_alarm_id), None)
                assert old_alarm is not None, rows
                assert old_alarm['delivery']['pushover']['status'] == 'sent'
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
