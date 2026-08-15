from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "pdl" / "backup-pager.sh"


class BackupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.backups = root / "backups"
        self.config = root / "etc"
        self.bin = root / "bin"
        for directory in (self.state, self.backups, self.config, self.bin):
            directory.mkdir(parents=True, exist_ok=True)
        (self.state / "pager.db").write_text("test database payload", encoding="utf-8")

        sqlite = self.bin / "sqlite3"
        sqlite.write_text(
            "#!/usr/bin/env python3\n"
            "import os, shutil, sys, time\n"
            "target = sys.argv[3].removeprefix('.backup ').strip().strip(chr(39))\n"
            "time.sleep(float(os.environ.get('FAKE_SQLITE_SLEEP', '0')))\n"
            "shutil.copyfile(sys.argv[1], target)\n",
            encoding="utf-8",
        )
        sqlite.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:{self.env.get('PATH', '')}",
                "PAGER_STATE_ROOT": str(self.state),
                "PAGER_DB_PATH": str(self.state / "pager.db"),
                "PAGER_BACKUP_DIR": str(self.backups),
                "PAGER_CONFIG_DIR": str(self.config),
                "PAGER_BACKUP_LOCK_FILE": str(Path(self.tmp.name) / "backup.lock"),
                "PAGER_BACKUP_RETENTION_DAYS": "14",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_backup(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_concurrent_backup_is_rejected(self) -> None:
        first_env = self.env.copy()
        first_env["FAKE_SQLITE_SLEEP"] = "1"
        first = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=first_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(0.2)
            second = self.run_backup()
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("backup kører allerede", second.stderr)
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, msg=stderr or stdout)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait(timeout=5)

        archives = list(self.backups.glob("racher-pager-*.tar.gz"))
        self.assertEqual(len(archives), 1)

    def test_rapid_sequential_backups_never_overwrite(self) -> None:
        first = self.run_backup()
        self.assertEqual(first.returncode, 0, msg=first.stderr or first.stdout)
        second = self.run_backup()
        self.assertEqual(second.returncode, 0, msg=second.stderr or second.stdout)
        archives = sorted(self.backups.glob("racher-pager-*.tar.gz"))
        self.assertEqual(len(archives), 2)
        self.assertNotEqual(archives[0].name, archives[1].name)


if __name__ == "__main__":
    unittest.main()
