"""Exercise update/rollback boundaries without Docker, SMS or a live server."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/update-sbr-pager.sh"
FAKE_COMMAND = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["UPDATE_TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
failure = os.getenv("UPDATE_TEST_FAILURE", "")
if name == "id":
    print("0")
elif name == "git":
    print("test-checkout")
elif name == "systemctl":
    if args[0] == "is-active":
        sys.exit(0 if args[-1] in {"sbr-pager-watchdog.timer", "sbr-pager-watchdog.service"} else 3)
elif name == "docker":
    if args[0] == "compose":
        if "config" in args and failure == "config": sys.exit(1)
        if "build" in args and failure == "build": sys.exit(1)
        if "up" in args and failure == "startup" and not any("rollback-" in part for part in args): sys.exit(1)
    elif args[0] == "inspect":
        print("sha256:old-image")
    elif args[0] == "cp":
        Path(args[-1]).write_bytes(b"test-backup")
    elif args[0] == "exec" and "python" in args:
        code = args[args.index("-c") + 1]
        if "sqlite3" in code and failure == "backup": sys.exit(1)
        if "urllib.request" in code and failure == "health": sys.exit(1)
'''


def run_update(tmp_path, driver="usb", failure=""):
    application = tmp_path / "app"
    application.mkdir()
    (application / ".env").write_text("SMS_MODEM_DRIVER=" + driver + "\nCUDY_PASSWORD=test-only\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("docker", "systemctl", "id", "git", "sleep"):
        command = commands / name
        command.write_text("#!" + sys.executable + "\n" + FAKE_COMMAND)
        command.chmod(0o755)
    log = tmp_path / "operations.jsonl"
    environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                       SBR_PAGER_ROOT=str(application), UPDATE_TEST_LOG=str(log),
                       UPDATE_TEST_FAILURE=failure)
    result = subprocess.run(["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, timeout=15)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls, application


def recreations(calls):
    return [call for call in calls if call[:2] == ["docker", "compose"] and "up" in call]


@pytest.mark.parametrize("driver,filename", [("usb", "docker-compose.yml"), ("cudy", "cudy.yml")])
def test_update_preserves_openwa_and_restores_only_previously_active_watchdog(tmp_path, driver, filename):
    result, calls, application = run_update(tmp_path, driver)
    assert result.returncode == 0, result.stderr
    updates = recreations(calls)
    assert [call[-1] for call in updates] == ["sms-whatsapp", "sms-gateway"]
    assert any(part.endswith("compose/sms-gateway/" + filename) for part in updates[1])
    assert all("--no-deps" in call for call in updates)
    assert ["systemctl", "stop", "sbr-pager-watchdog.service"] in calls
    assert ["systemctl", "start", "sbr-pager-watchdog.timer"] in calls
    assert ["systemctl", "start", "sbr-modem-watchdog.timer"] not in calls
    backup = next((application / "manual-backups").iterdir())
    assert (backup / "sms-whatsapp.db").exists() and (backup / "sms-gateway.db").exists()
    assert (backup / "env.backup").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("failure", ["config", "backup", "build"])
def test_validation_backup_or_build_failure_never_recreates_running_services(tmp_path, failure):
    result, calls, _ = run_update(tmp_path, failure=failure)
    assert result.returncode != 0
    assert not recreations(calls)
    assert not any(call[:2] == ["systemctl", "stop"] for call in calls)


@pytest.mark.parametrize("failure", ["health", "startup"])
def test_failed_startup_rolls_back_both_images_and_restores_watchdog(tmp_path, failure):
    result, calls, _ = run_update(tmp_path, failure=failure)
    assert result.returncode != 0
    rollbacks = [call for call in recreations(calls) if any("rollback-" in part for part in call)]
    assert [call[-1] for call in rollbacks] == ["sms-whatsapp", "sms-gateway"]
    assert all("--no-build" in call and "--no-deps" in call for call in rollbacks)
    assert ["systemctl", "start", "sbr-pager-watchdog.timer"] in calls
