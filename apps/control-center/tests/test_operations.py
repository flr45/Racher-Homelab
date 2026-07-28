import hashlib
import json
import sys
from pathlib import Path

from flask import Flask

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CONTROL_CENTER_ROOT.parents[1]
if str(CONTROL_CENTER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_CENTER_ROOT))

from services.backup_service import (  # noqa: E402
    BackupNotFoundError,
    backups,
    validate_backup,
)


def configured_app(backup_root):
    app = Flask(__name__)
    app.config["BACKUP_ROOT"] = backup_root
    return app


def write_backup(root, name="2026-01-01_12-00-00", *, corrupt=False):
    backup = root / name
    backup.mkdir(parents=True)
    files = {
        "MANIFEST.json": json.dumps(
            {
                "format_version": 1,
                "created_at": "2026-01-01T12:00:00+00:00",
                "control_center_volume": "racher-control-center_control-center-data",
            }
        ).encode(),
        "postgres.dump": b"postgres",
        "npm-database.sql.gz": b"npm",
        "control-center-data.tar.gz": b"control-center",
    }
    for filename, content in files.items():
        (backup / filename).write_bytes(content)
    checksum_lines = []
    for filename in sorted(files):
        digest = hashlib.sha256(files[filename]).hexdigest()
        checksum_lines.append(f"{digest}  {filename}")
    (backup / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n")
    if corrupt:
        (backup / "control-center-data.tar.gz").write_bytes(b"changed")
    return backup


def test_backup_validation_accepts_complete_verified_backup(tmp_path):
    backup_root = tmp_path / "backups"
    write_backup(backup_root)

    with configured_app(backup_root).app_context():
        result = validate_backup("2026-01-01_12-00-00")
        listed = backups()

    assert result["valid"] is True
    assert result["missing"] == []
    assert result["errors"] == []
    assert "control-center-data.tar.gz" in result["checked_files"]
    assert listed[0]["has_manifest"] is True
    assert listed[0]["has_checksums"] is True
    assert listed[0]["format_version"] == 1


def test_backup_validation_detects_corruption(tmp_path):
    backup_root = tmp_path / "backups"
    write_backup(backup_root, corrupt=True)

    with configured_app(backup_root).app_context():
        result = validate_backup("2026-01-01_12-00-00")

    assert result["valid"] is False
    assert "Checksum fejlede: control-center-data.tar.gz" in result["errors"]


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

    assert "control-center-data.tar.gz" in restore_script
    assert "--dry-run" in restore_script
    assert "--stage-control-center" in restore_script
    assert "docker volume create" in restore_script
    assert "Den aktive volume er ikke ændret" in restore_script
    assert 'backup_volume "$CONTROL_CENTER_VOLUME" control-center-data true' in backup_script
    assert "MANIFEST.json" in backup_script
    assert "SHA256SUMS" in backup_script
    assert "compose/control-center" in update_script
    assert "config --quiet" in update_script
