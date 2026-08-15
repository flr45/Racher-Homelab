import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import gateway_watchdog as watchdog


class GatewayWatchdogTests(unittest.TestCase):
    def test_failure_counter_is_persistent_and_resettable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "failures"
            self.assertEqual(watchdog.read_failures(path), 0)
            watchdog.write_failures(2, path)
            self.assertEqual(watchdog.read_failures(path), 2)
            watchdog.write_failures(0, path)
            self.assertEqual(watchdog.read_failures(path), 0)

    def test_restart_gateway_uses_fixed_docker_argv(self):
        run = Mock()
        run.return_value = Mock(returncode=0)
        self.assertTrue(watchdog.restart_gateway(run))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/docker", "restart", "racher-pager-gateway"])
        self.assertNotIn("sh", argv)
        self.assertNotIn("-c", argv)

    def test_maintenance_lock_detects_an_active_holder(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maintenance.lock"
            first = path.open("a+")
            try:
                fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                active, handle = watchdog.maintenance_in_progress(path)
                self.assertTrue(active)
                self.assertIsNone(handle)
            finally:
                fcntl.flock(first.fileno(), fcntl.LOCK_UN)
                first.close()

            active, handle = watchdog.maintenance_in_progress(path)
            self.assertFalse(active)
            self.assertIsNotNone(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


if __name__ == "__main__":
    unittest.main()
