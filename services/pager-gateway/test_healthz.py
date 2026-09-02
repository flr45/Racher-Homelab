from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class HealthcheckTests(unittest.TestCase):
    def test_healthcheck_dependency_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import os
                import sqlite3
                from unittest.mock import patch

                os.environ['PAGER_COOKIE_SECURE'] = '0'
                import app

                app.app.config.update(TESTING=True)
                client = app.app.test_client()
                old_state = app.source._status
                try:
                    app.storage.update_settings({'source_mode': 'pdl-file'})

                    app.source._status = 'waiting'
                    waiting = client.get('/healthz')
                    assert waiting.status_code == 200, waiting.get_data(as_text=True)
                    assert waiting.get_json()['ok'] is True

                    app.source._status = 'running'
                    running = client.get('/healthz')
                    assert running.status_code == 200, running.get_data(as_text=True)
                    assert running.get_json()['ok'] is True

                    app.source._status = 'error'
                    failed_source = client.get('/healthz')
                    assert failed_source.status_code == 503, failed_source.get_data(as_text=True)
                    assert failed_source.get_json()['ok'] is False

                    with patch.object(app.storage, 'connect', side_effect=sqlite3.OperationalError('locked')):
                        failed_db = client.get('/healthz')
                    assert failed_db.status_code == 503, failed_db.get_data(as_text=True)
                    assert failed_db.get_json()['ok'] is False
                    assert failed_db.get_json()['database'] == 'error'
                finally:
                    app.source._status = old_state
                    app.source.stop()
                """
            )
            env = os.environ.copy()
            env['PAGER_DATA_DIR'] = tmp
            env['PAGER_DB_PATH'] = str(Path(tmp) / 'pager-health-test.db')
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
