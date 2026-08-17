import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PdlCursorSeedTests(unittest.TestCase):
    def test_seed_is_once_only_and_points_at_current_eof(self):
        service_dir = Path(__file__).resolve().parent
        helper = service_dir / "pdl" / "seed-pdl-cursor.py"

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pdl.log"
            log.write_text("historic\n", encoding="utf-8")

            first = subprocess.run(
                [sys.executable, str(helper), str(log)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            cursor = Path(str(log) + ".racher-cursor")
            payload = json.loads(cursor.read_text(encoding="utf-8"))
            self.assertEqual(payload["offset"], len("historic\n"))
            first_inode = payload["ino"]

            log.write_text("historic\nnew\n", encoding="utf-8")
            second = subprocess.run(
                [sys.executable, str(helper), str(log)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            unchanged = json.loads(cursor.read_text(encoding="utf-8"))
            self.assertEqual(unchanged["offset"], len("historic\n"))
            self.assertEqual(unchanged["ino"], first_inode)

    def test_missing_log_is_nonfatal(self):
        service_dir = Path(__file__).resolve().parent
        helper = service_dir / "pdl" / "seed-pdl-cursor.py"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "missing.log"
            result = subprocess.run(
                [sys.executable, str(helper), str(log)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(Path(str(log) + ".racher-cursor").exists())


if __name__ == "__main__":
    unittest.main()
