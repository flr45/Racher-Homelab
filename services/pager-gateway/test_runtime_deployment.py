from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PDL = ROOT / "pdl"


class RuntimeDeploymentTests(unittest.TestCase):
    def test_system_agent_installer_refreshes_host_executed_files(self):
        script = (PDL / "install-system-agent.sh").read_text(encoding="utf-8")
        self.assertIn("configure-pdl.sh run-pdl-headless.sh", script)
        self.assertIn('network_portal.py" "$NETWORK_DIR/network_portal.py', script)
        self.assertIn("TimeoutStartSec=150s", script)
        self.assertIn("systemctl reset-failed racher-pdl.service", script)
        self.assertIn("systemctl restart racher-pdl.service", script)

    def test_shared_sqlite_state_is_group_writable_for_root_agents_and_web_runtime(self):
        script = (PDL / "install-system-agent.sh").read_text(encoding="utf-8")
        self.assertIn('chmod 2770 "$DATA_DIR"', script)
        self.assertGreaterEqual(script.count("UMask=0007"), 2)
        compose_helper = (PDL / "pager-compose.sh").read_text(encoding="utf-8")
        self.assertIn("PAGER_RUNTIME_UID", compose_helper)
        self.assertIn("PAGER_RUNTIME_GID", compose_helper)
        self.assertIn("vapid-private.pem", compose_helper)
        self.assertIn("pdl.log.racher-cursor", compose_helper)
        self.assertIn("Afviser usikker symlink", compose_helper)

    def test_update_validates_recovery_layers_and_restores_host_files(self):
        script = (PDL / "update-pager.sh").read_text(encoding="utf-8")
        self.assertIn("restore_host_runtime_from_checkout", script)
        self.assertIn("gateway_watchdog.py", script)
        self.assertIn("fsk_status_agent.py", script)
        self.assertIn("external_monitor.py", script)
        self.assertIn("systemctl is-active --quiet racher-pager-system-agent.service", script)
        self.assertIn("systemctl is-active --quiet racher-pager-gateway-watchdog.timer", script)
        self.assertIn("systemctl is-active --quiet racher-pdl.service", script)
        self.assertIn("racher-pager-post-update", script)
        self.assertIn("--force-recreate pager-gateway", script)

    def test_manual_rollback_restores_non_container_runtime(self):
        script = (PDL / "rollback-pager.sh").read_text(encoding="utf-8")
        self.assertIn("configure-pdl.sh", script)
        self.assertIn("run-pdl-headless.sh", script)
        self.assertIn("network_portal.py", script)
        self.assertIn("install-system-agent.sh", script)

    def test_restore_preserves_machine_identity_on_existing_pi(self):
        script = (PDL / "restore-pager.sh").read_text(encoding="utf-8")
        self.assertIn("CURRENT_MONITOR_KEY", script)
        self.assertIn("PAGER_RESTORE_MONITOR_KEY", script)
        self.assertIn("ON CONFLICT(key) DO UPDATE", script)
        self.assertIn('! -e "$destination"', script)
        self.assertIn("pdl.env gateway.env network.env cloudflared.token", script)
        self.assertIn("systemctl reset-failed racher-pdl.service", script)
        self.assertIn("STATE_UID", script)
        self.assertIn("STATE_GID", script)
        self.assertIn('chown "$STATE_UID:$STATE_GID" "$STATE_ROOT/pager.db"', script)

    def test_restore_quiesces_sqlite_writers_and_removes_wal_sidecars(self):
        script = (PDL / "restore-pager.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl stop racher-pager-fsk-status.timer", script)
        self.assertIn("systemctl stop racher-pager-fsk-status.service", script)
        self.assertIn('rm -f "$STATE_ROOT/pager.db-wal" "$STATE_ROOT/pager.db-shm"', script)
        self.assertIn("RUNTIME_PAUSED=1", script)
        self.assertIn("RUNTIME_PAUSED=0", script)
        self.assertIn("systemctl start racher-pager-fsk-status.timer", script)
        self.assertIn("maintenance flock", script)

    def test_pdl_wrapper_waits_for_pinned_or_ftdi_device(self):
        script = (PDL / "run-pdl-headless.sh").read_text(encoding="utf-8")
        self.assertIn("select_fsk_device", script)
        self.assertIn("pinnede enhed", script)
        self.assertIn("*ftdi*", script)
        self.assertIn("*ft232*", script)
        self.assertIn("venter roligt på hardware", script)
        self.assertIn('sleep "$DEVICE_WAIT_SECONDS"', script)

    def test_external_monitor_update_reuses_saved_secret_without_printing_it(self):
        script = (ROOT / "install-external-monitor.sh").read_text(encoding="utf-8")
        self.assertIn("SAVED_KEY", script)
        self.assertIn("existing_value PAGER_MONITOR_KEY", script)
        self.assertIn('unset SAVED_KEY', script)
        self.assertIn('unset MONITOR_KEY', script)
        self.assertIn("Monitor-key: konfigureret (ikke vist)", script)
        self.assertNotIn('echo "Monitor-key: $MONITOR_KEY"', script)
        self.assertIn("systemctl start racher-pager-external-monitor.service", script)


if __name__ == "__main__":
    unittest.main()
