from pathlib import Path

from app import create_app
from services.backup_service import BackupNotFoundError, validate_backup

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


def test_operations_scripts_cover_control_center_and_safe_restore():
    backup_script = (REPOSITORY_ROOT / "scripts/backup.sh").read_text()
    restore_script = (REPOSITORY_ROOT / "scripts/restore.sh").read_text()
    update_script = (REPOSITORY_ROOT / "scripts/update-stacks.sh").read_text()

    assert "control-center-data.tar.gz" in backup_script
    assert "source.backup(target)" in backup_script
    assert "CONTROL_CENTER_DATA_DIR" in backup_script
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
