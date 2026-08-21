from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class FirstAdminSetupRaceTests(unittest.TestCase):
    def test_concurrent_setup_requests_create_only_one_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import threading
                import time

                import app

                app.app.config.update(TESTING=True)
                clients = [app.app.test_client(), app.app.test_client()]
                tokens = []
                for client in clients:
                    client.get('/setup')
                    with client.session_transaction() as sess:
                        tokens.append(sess['csrf_token'])

                original_user_count = app.storage.user_count
                def delayed_user_count():
                    count = original_user_count()
                    if count == 0:
                        time.sleep(0.2)
                    return count
                app.storage.user_count = delayed_user_count

                barrier = threading.Barrier(2)
                responses = []
                errors = []

                def submit(index):
                    try:
                        barrier.wait(timeout=5)
                        response = clients[index].post(
                            '/setup',
                            data={
                                'csrf_token': tokens[index],
                                'display_name': f'Admin {index}',
                                'username': f'admin{index}',
                                'password': 'meget-hemmelig-admin',
                            },
                        )
                        responses.append(response.status_code)
                    except Exception as exc:
                        errors.append(repr(exc))

                threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

                try:
                    assert not errors, errors
                    assert len(responses) == 2, responses
                    assert all(code == 302 for code in responses), responses
                    assert app.storage.user_count() == 1, app.storage.user_count()
                finally:
                    app.source.stop()
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
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
