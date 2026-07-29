import os
import sqlite3
from pathlib import Path

import docker


def _check(check_id, title, status, message, *, required=True):
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "message": message,
        "required": required,
    }


def _directory_check(check_id, title, path, *, required=True):
    target = Path(path)
    if not target.exists():
        return _check(check_id, title, "failed" if required else "warning", "Mappen findes ikke.", required=required)
    if not target.is_dir():
        return _check(check_id, title, "failed", "Stien er ikke en mappe.", required=required)
    if not os.access(target, os.R_OK | os.W_OK | os.X_OK):
        return _check(check_id, title, "failed", "Mappen kan ikke læses og skrives af processen.", required=required)
    return _check(check_id, title, "passed", "Mappen er tilgængelig.", required=required)


def _database_check(database_path):
    path = Path(database_path)
    if not path.exists():
        return _check("database", "SQLite database", "warning", "Databasen er endnu ikke oprettet.")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        result = connection.execute("PRAGMA quick_check").fetchone()
        connection.close()
    except sqlite3.Error:
        return _check("database", "SQLite database", "failed", "Databasen kunne ikke valideres.")
    if not result or result[0] != "ok":
        return _check("database", "SQLite database", "failed", "SQLite quick_check fejlede.")
    return _check("database", "SQLite database", "passed", "Databasen bestod SQLite quick_check.")


def _docker_check(client_factory):
    try:
        client = client_factory()
        client.ping()
        version = client.version().get("Version")
    except Exception:
        return _check("docker", "Docker Engine", "failed", "Docker Engine kan ikke kontaktes.")
    message = f"Docker Engine svarer{f' med version {version}' if version else ''}."
    return _check("docker", "Docker Engine", "passed", message)


def _secret_check(config):
    configured = bool(config.get("SECRET_KEY"))
    return _check(
        "secret-key",
        "Applikationshemmelighed",
        "passed" if configured else "failed",
        "Permanent secret key er konfigureret." if configured else "RACHER_OS_SECRET_KEY mangler.",
    )


def _access_check(config):
    admins = config.get("ALLOWED_EMAILS") or set()
    return _check(
        "admin-access",
        "Administratoradgang",
        "passed" if admins else "warning",
        f"{len(admins)} administrator(er) er allowlistet." if admins else "Ingen administrator-email er allowlistet.",
    )


def _integration_check(check_id, title, configured, message):
    return _check(check_id, title, "passed" if configured else "warning", message, required=False)


def build_readiness_report(config, *, docker_client_factory=docker.from_env):
    checks = [
        _directory_check("data-root", "Permanent datamappe", config["DATA_ROOT"]),
        _directory_check("backup-root", "Backupmappe", config["BACKUP_ROOT"]),
        _directory_check("plugin-root", "Pluginmappe", config["PLUGIN_ROOT"], required=False),
        _database_check(config["DATABASE_PATH"]),
        _docker_check(docker_client_factory),
        _secret_check(config),
        _access_check(config),
        _integration_check(
            "cloudflare",
            "Cloudflare integration",
            bool(config.get("CLOUDFLARE_API_TOKEN") and config.get("CLOUDFLARE_ACCOUNT_ID")),
            "Cloudflare credentials er konfigureret." if config.get("CLOUDFLARE_API_TOKEN") and config.get("CLOUDFLARE_ACCOUNT_ID") else "Cloudflare credentials er ikke komplette.",
        ),
        _integration_check(
            "ssh-console",
            "SSH Console",
            bool(config.get("SSH_KNOWN_HOSTS_PATH") and config.get("SSH_IDENTITY_FILE") and config.get("SSH_CONSOLE_HOSTS")),
            "SSH host-key-fil, identity og hosts er konfigureret." if config.get("SSH_KNOWN_HOSTS_PATH") and config.get("SSH_IDENTITY_FILE") and config.get("SSH_CONSOLE_HOSTS") else "SSH Console er ikke fuldt konfigureret.",
        ),
        _integration_check(
            "notifications",
            "Notification Center",
            bool(config.get("NOTIFICATION_WEBHOOK_URL") or (config.get("PUSHOVER_APP_TOKEN") and config.get("PUSHOVER_USER_KEY"))),
            "Mindst én notifikationskanal er konfigureret." if config.get("NOTIFICATION_WEBHOOK_URL") or (config.get("PUSHOVER_APP_TOKEN") and config.get("PUSHOVER_USER_KEY")) else "Ingen notifikationskanal er konfigureret.",
        ),
    ]

    required = [item for item in checks if item["required"]]
    failed_required = [item for item in required if item["status"] == "failed"]
    warnings = [item for item in checks if item["status"] == "warning"]
    passed = [item for item in checks if item["status"] == "passed"]
    score = round((len(passed) / len(checks)) * 100) if checks else 0
    state = "ready" if not failed_required else "blocked"
    return {
        "state": state,
        "score": score,
        "summary": {
            "total": len(checks),
            "passed": len(passed),
            "warnings": len(warnings),
            "failed": len([item for item in checks if item["status"] == "failed"]),
            "required_failed": len(failed_required),
        },
        "checks": checks,
        "version": str(config.get("RACHER_OS_VERSION") or "unknown"),
    }
