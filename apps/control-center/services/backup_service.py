from datetime import datetime, timezone

from flask import current_app


def directory_size(path):
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except Exception:
        return 0


def backups(limit=20):
    try:
        backup_root = current_app.config["BACKUP_ROOT"]
        candidates = [path for path in backup_root.iterdir() if path.is_dir()]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [
            {
                "name": path.name,
                "time": datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%d-%m-%Y %H:%M"
                ),
                "recorded_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat(),
                "size_mb": round(directory_size(path) / 1024 / 1024, 1),
            }
            for path in candidates[:limit]
        ]
    except Exception:
        return []


def newest_backup():
    items = backups(limit=1)
    return items[0] if items else None
