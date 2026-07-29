import hashlib
import json
from pathlib import Path

from app import create_app
from services.backup_service import BackupNotFoundError, backups, validate_backup

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def configured_app(backup_root):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "BACKUP_ROOT": backup_root,
        }
    )


def test_backup_validation_rejects_path_traversal(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    with configured_app(backup_root).app_context():
        try:
            validate_backup("../outside")
        except BackupNotFoundError:
            pass
        else:
            raise AssertionError("Path traversal should be rejected")


def test_backup_validation_accepts_format_two_and_ignores_latest_symlink(tmp_path):
    backup_root = tmp_path / "backups"
    backup_dir = backup_root / "2026-07-29_15-13-26"
    backup_dir.mkdir(parents=True)

    payloads = {
        "MANIFEST.json": json.dumps({"format_version": 2}).encode(),
        "postgres.dump": b"postgres",
        "npm-database.sql.gz": b"mariadb",
        "control-center-data.tar.gz": b"control-center",
    }
    for filename, content in payloads.items():
        (backup_dir / filename).write_bytes(content)

    checksum_lines = [
        f"{hashlib.sha256(content).hexdigest()}  {filename}"
        for filename, content in sorted(payloads.items())
    ]
    (backup_dir / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n")
    (backup_root / "latest").symlink_to(backup_dir)

    with configured_app(backup_root).app_context():
        result = validate_backup(backup_dir.name)
        discovered = backups()

    assert result["valid"] is True
    assert [item["name"] for item in discovered] == [backup_dir.name]


def test_operations_scripts_cover_control_center_and_safe_restore():
    backup_script = (REPOSITORY_ROOT / "scripts/backup.sh").read_text()
    restore_script = (REPOSITORY_ROOT / "scripts/restore.sh").read_text()
    update_script = (REPOSITORY_ROOT / "scripts/update-stacks.sh").read_text()

    assert "control-center-data.tar.gz" in backup_script
    assert "source.backup(target)" in backup_script
    assert "CONTROL_CENTER_DATA_DIR" in backup_script
    assert "CONTROL_CENTER_GID" in backup_script
    assert "normalize_backup_permissions" in backup_script
    assert "read_env_value" in backup_script
    assert 'source "$ENV_FILE"' not in backup_script
    assert "control-center-data.tar.gz" in restore_script
    assert "--dry-run" in restore_script
    assert "--stage-control-center" in restore_script
    assert "docker volume create" in restore_script
    assert "Den aktive volume er ikke ændret" in restore_script
    assert "MANIFEST.json" in backup_script
    assert "SHA256SUMS" in backup_script
    assert "compose/control-center" in update_script
    assert "config --quiet" in update_script
