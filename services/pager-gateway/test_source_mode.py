from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class SourceModeTests(unittest.TestCase):
    def test_mock_mode_consumes_pdl_lines_without_live_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                """
                import os
                os.environ['PAGER_COOKIE_SECURE'] = '0'

                import app

                try:
                    app.storage.update_settings({'source_mode': 'mock'})
                    before = app.storage.message_count()
                    app.source.on_line('1234567 12:00:00 15-08-26 POCSAG-1 ALPHA 1200 Station Slagelse Test')
                    after_mock = app.storage.message_count()

                    app.storage.update_settings({'source_mode': 'pdl-file'})
                    app.source.on_line('1234567 12:00:05 15-08-26 POCSAG-1 ALPHA 1200 Station Slagelse Test 2')
                    after_pdl = app.storage.message_count()

                    app.storage.update_settings({'source_mode': 'mock'})
                    app.source.on_line('1234567 12:00:10 15-08-26 POCSAG-1 ALPHA 1200 Station Slagelse Test 3')
                    after_second_mock = app.storage.message_count()

                    assert after_mock == before, (before, after_mock)
                    assert after_pdl == before + 1, (before, after_pdl)
                    assert after_second_mock == after_pdl, (after_pdl, after_second_mock)
                finally:
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
