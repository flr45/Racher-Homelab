import gzip
import hashlib
import json

from flask import Flask

from backup_verification_extension import init_backup_verification_center
from rbac_extension import init_rbac

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


def make_app(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        BACKUP_ROOT=backup_root,
        BACKUP_MAX_AGE_HOURS=36,
        ALLOWED_EMAILS={"admin@example.com"},
        RBAC_VIEWER_EMAILS={"viewer@example.com"},
    )
    init_rbac(app)
    init_backup_verification_center(app)
    return app


def create_valid_backup(root):
    backup = root / "2026-07-29_10-00-00"
    backup.mkdir()
    (backup / "MANIFEST.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )
    (backup / "postgres.dump").write_bytes(b"postgres")
    with gzip.open(backup / "npm-database.sql.gz", "wb") as handle:
        handle.write(b"database")
    (backup / "control-center-data.tar.gz").write_bytes(b"archive")
    names = [
        "MANIFEST.json",
        "postgres.dump",
        "npm-database.sql.gz",
        "control-center-data.tar.gz",
    ]
    lines = []
    for name in names:
        digest = hashlib.sha256((backup / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (backup / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_backup_verification_requires_read_permission(tmp_path):
    response = make_app(tmp_path).test_client().get("/api/backup-verification")
    assert response.status_code == 403


def test_missing_backup_is_reported_read_only(tmp_path):
    response = make_app(tmp_path).test_client().get(
        "/api/backup-verification", headers=VIEWER_HEADERS
    )
    report = response.get_json()["report"]
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert report["status"] == "missing"
    assert report["read_only"] is True


def test_valid_backup_is_verified(tmp_path):
    app = make_app(tmp_path)
    create_valid_backup(app.config["BACKUP_ROOT"])
    response = app.test_client().get("/api/backup-verification", headers=VIEWER_HEADERS)
    report = response.get_json()["report"]
    assert report["status"] == "verified"
    assert report["validation"]["valid"] is True
    assert "manifest" not in report["validation"]
