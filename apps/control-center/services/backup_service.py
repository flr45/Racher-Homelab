import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app


class BackupNotFoundError(Exception):
    """Raised when a requested backup does not exist below BACKUP_ROOT."""


def directory_size(path):
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except Exception:
        return 0


def resolve_backup(name):
    if not name or name in {".", ".."} or Path(name).name != name:
        raise BackupNotFoundError(name)
    backup_root = current_app.config["BACKUP_ROOT"].resolve()
    candidate = (backup_root / name).resolve()
    if candidate.parent != backup_root or not candidate.is_dir():
        raise BackupNotFoundError(name)
    return candidate


def load_manifest(path):
    manifest_path = path / "MANIFEST.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def backup_summary(path):
    manifest = load_manifest(path)
    return {
        "name": path.name,
        "time": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d-%m-%Y %H:%M"),
        "recorded_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "size_mb": round(directory_size(path) / 1024 / 1024, 1),
        "has_manifest": manifest is not None,
        "has_checksums": (path / "SHA256SUMS").is_file(),
        "format_version": manifest.get("format_version") if manifest else None,
    }


def backups(limit=20):
    try:
        backup_root = current_app.config["BACKUP_ROOT"]
        candidates = [
            path
            for path in backup_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [backup_summary(path) for path in candidates[:limit]]
    except Exception:
        return []


def newest_backup():
    items = backups(limit=1)
    return items[0] if items else None


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksums(path):
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, filename = line.partition("  ")
        if not separator or len(digest) != 64 or Path(filename).name != filename:
            raise ValueError("Ugyldig checksumlinje")
        entries.append((digest.lower(), filename))
    return entries


def validate_backup(name):
    path = resolve_backup(name)
    manifest = load_manifest(path)
    checksum_path = path / "SHA256SUMS"
    required = {
        "MANIFEST.json",
        "SHA256SUMS",
        "postgres.dump",
        "npm-database.sql.gz",
        "control-center-data.tar.gz",
    }
    missing = sorted(filename for filename in required if not (path / filename).is_file())
    errors = []
    checked_files = []

    if manifest is None:
        errors.append("Manifest mangler eller er ugyldigt.")
    elif manifest.get("format_version") not in {1, 2}:
        errors.append("Backupformatet understøttes ikke.")

    if checksum_path.is_file():
        try:
            for expected, filename in parse_checksums(checksum_path):
                target = path / filename
                if not target.is_file():
                    errors.append(f"Checksumfil mangler: {filename}")
                    continue
                actual = sha256_file(target)
                checked_files.append(filename)
                if actual != expected:
                    errors.append(f"Checksum fejlede: {filename}")
        except (OSError, ValueError) as exc:
            errors.append(f"Checksumlisten er ugyldig: {exc}")
    else:
        errors.append("SHA256SUMS mangler.")

    return {
        "name": name,
        "valid": not missing and not errors,
        "missing": missing,
        "errors": errors,
        "checked_files": sorted(checked_files),
        "manifest": manifest,
    }
