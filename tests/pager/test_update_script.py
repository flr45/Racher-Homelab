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
        if "{{.State.Running}}" in args:
            print("false" if os.getenv("UPDATE_TEST_STOPPED") == "true" and args[1] == "racher-sms-gateway" else "true")
        else: print("sha256:old-image")
    elif args[0] == "cp":
        if args[1].endswith("/data/."):
            (Path(args[-1]) / "sms-gateway.db").write_bytes(b"test-backup")
        else: Path(args[-1]).write_bytes(b"test-backup")
    elif args[0] == "run":
        if failure == "backup": sys.exit(1)
        mount = args[args.index("--mount") + 1]
        directory = mount.split("src=", 1)[1].split(",dst=", 1)[0]
        (Path(directory) / "consistent.db").write_bytes(b"consistent-backup")
    elif args[0] == "exec" and "python" in args:
        code = args[args.index("-c") + 1]
        if "sqlite3" in code and failure == "backup": sys.exit(1)
        if "urllib.request" in code and failure == "health": sys.exit(1)
'''


def run_update(tmp_path, driver="usb", failure="", offsite=False, stopped=False, rollback_driver=None, target_driver=None):
    application = tmp_path / "app"
    application.mkdir()
    (application / ".env").write_text("SMS_MODEM_DRIVER=" + driver + "\nCUDY_PASSWORD=test-only\n" + ("SMS_WHATSAPP_OFFSITE_MOUNT=true\n" if offsite else ""))
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
    environment["UPDATE_TEST_STOPPED"] = "true" if stopped else "false"
    if target_driver:
        environment["SBR_PAGER_TARGET_DRIVER"] = target_driver
    if rollback_driver:
        original = tmp_path / "original.env"
        original.write_text("SMS_MODEM_DRIVER=" + rollback_driver + "\n")
        environment["SBR_PAGER_ROLLBACK_ENV"] = str(original)
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
    if failure == "config":
        assert not any(call[:2] == ["systemctl", "stop"] for call in calls)
    else:
        assert ["systemctl", "start", "sbr-pager-watchdog.timer"] in calls


@pytest.mark.parametrize("failure", ["health", "startup"])
def test_failed_startup_rolls_back_both_images_and_restores_watchdog(tmp_path, failure):
    result, calls, _ = run_update(tmp_path, failure=failure)
    assert result.returncode != 0
    rollbacks = [call for call in recreations(calls) if any("rollback-" in part for part in call)]
    assert [call[-1] for call in rollbacks] == ["sms-whatsapp", "sms-gateway"]
    assert all("--no-build" in call and "--no-deps" in call for call in rollbacks)
    assert ["systemctl", "start", "sbr-pager-watchdog.timer"] in calls


@pytest.mark.parametrize("failure", ["", "startup"])
def test_update_and_rollback_keep_configured_offsite_mount(tmp_path, failure):
    result, calls, _ = run_update(tmp_path, failure=failure, offsite=True)
    pager_calls = [call for call in calls if call[:2] == ["docker", "compose"] and (call[-1] == "sms-whatsapp" or "config" in call and any("sms-whatsapp/compose.yml" in x for x in call))]
    assert pager_calls and all(any(x.endswith("sms-whatsapp/offsite-backup.yml") for x in call) for call in pager_calls)
    assert result.returncode == (0 if not failure else 1)


def test_stopped_usb_gateway_backup_does_not_start_reader(tmp_path):
    result, calls, application = run_update(tmp_path, driver="cudy", stopped=True, rollback_driver="usb")
    assert result.returncode == 0, result.stderr
    helpers = [call for call in calls if call[:2] == ["docker", "run"]]
    assert len(helpers) == 1
    assert "none" in helpers[0] and "--read-only" in helpers[0]
    assert helpers[0][helpers[0].index("--entrypoint") + 1] == "python"
    assert not any(call[:3] == ["docker", "exec", "racher-sms-gateway"] and "sqlite3" in " ".join(call) for call in calls)
    backup = next((application / "manual-backups").iterdir())
    assert (backup / "sms-gateway.db").read_bytes() == b"consistent-backup"
    assert not (backup / "racher-sms-gateway-source").exists()
    assert (backup / "env.backup").read_text() == "SMS_MODEM_DRIVER=usb\n"


def test_usb_to_cudy_failure_restores_original_environment_and_transport(tmp_path):
    result, calls, application = run_update(tmp_path, driver="cudy", failure="startup", stopped=True, rollback_driver="usb")
    assert result.returncode == 1
    assert (application / ".env").read_text() == "SMS_MODEM_DRIVER=usb\n"
    rollback = [call for call in recreations(calls) if call[-1] == "sms-gateway" and any("rollback-" in x for x in call)]
    assert len(rollback) == 1
    assert any(x.endswith("sms-gateway/docker-compose.yml") for x in rollback[0])
    assert any("stop" in call and call[-1] == "sms-gateway" for call in calls)


@pytest.mark.parametrize("failure", ["", "backup", "build", "startup"])
def test_target_driver_changes_only_in_controlled_update_and_rolls_back(tmp_path, failure):
    result, calls, application = run_update(tmp_path, driver="usb", target_driver="cudy", stopped=True, failure=failure)
    assert result.returncode == (1 if failure else 0), result.stderr
    assert (application / ".env").read_text().splitlines()[-1] == ("CUDY_PASSWORD=test-only" if failure else "SMS_MODEM_DRIVER=cudy")
    if not failure:
        gateway = [call for call in recreations(calls) if call[-1] == "sms-gateway"]
        assert any(x.endswith("sms-gateway/cudy.yml") for x in gateway[0])
