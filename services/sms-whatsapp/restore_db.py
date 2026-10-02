"""Offline, same-schema SQLite recovery. Pager must be stopped first."""
import os
from pathlib import Path
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone


def schema(connection):
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {table: {"columns": [(r[1], r[2], r[3], r[5]) for r in connection.execute('PRAGMA table_info("' + table.replace('"','""') + '")')], "foreign_keys": list(connection.execute('PRAGMA foreign_key_list("' + table.replace('"','""') + '")'))} for table in tables}


def restore(source_path, target_path):
    source_path, target_path = Path(source_path), Path(target_path)
    if source_path.resolve() == target_path.resolve() or source_path.is_symlink() or target_path.is_symlink() or not source_path.is_file() or not target_path.is_file():
        raise ValueError("Backup og mål skal være to eksisterende, forskellige SQLite-filer")
    target_stat = target_path.stat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    temporary = target_path.with_name("restore-" + stamp + ".tmp")
    safety = target_path.with_name("pre-restore-" + stamp + ".sqlite")
    try:
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(target_path) as current:
            source.execute("PRAGMA trusted_schema=OFF")
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or source.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Backupens integritetskontrol fejlede")
            if schema(source) != schema(current):
                raise ValueError("Databaseversionerne passer ikke sammen; brug backupens programversion")
            with sqlite3.connect(safety) as safe:
                current.backup(safe)
                if safe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Sikkerhedsbackupen fejlede")
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
                destination.execute("PRAGMA trusted_schema=OFF")
                now = datetime.now(timezone.utc)
                # Never replay deliveries copied from a historical snapshot.
                destination.execute("UPDATE whats_app_delivery SET status='cancelled', error='Database gendannet; gammelt job genafsendes ikke' WHERE status IN ('pending','retrying','held','sending','failed')")
                destination.execute("UPDATE whatsapp_retry_state SET completed_at=?,next_attempt_at=NULL",(now.replace(tzinfo=None).isoformat(' '),))
                destination.execute("UPDATE single_whatsapp_test SET status='cancelled',error='Database gendannet',completed_at=? WHERE status IN ('pending','running')",(now.replace(tzinfo=None).isoformat(' '),))
                for key,value in (("operation_mode","maintenance"),("operation_until",(now+timedelta(minutes=30)).isoformat()),("operation_reason","Kontrollér den gendannede database"),("operator_auth_epoch",secrets.token_hex(32))):
                    destination.execute("INSERT INTO pager_runtime_setting(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value,now.replace(tzinfo=None).isoformat(' ')))
                destination.commit()
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Den gendannede database fejlede kontrollen")
        os.chmod(safety,0o600)
        os.chmod(temporary,0o600)
        # Only safe offline: no process may retain a database connection.
        target_path.with_name(target_path.name+'-wal').unlink(missing_ok=True)
        target_path.with_name(target_path.name+'-shm').unlink(missing_ok=True)
        if hasattr(os,"chown"):
            os.chown(temporary,target_stat.st_uid,target_stat.st_gid)
            os.chown(safety,target_stat.st_uid,target_stat.st_gid)
        temporary.replace(target_path)
        return safety
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    import sys
    print("Gendannet. Sikkerhedsbackup:",restore(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve()))
