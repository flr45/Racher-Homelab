from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

source = APP_PATH.read_text()
source = source.replace("import sqlite3\n", "")
source = source.replace(
    "from config import Config\n",
    "from config import Config\n"
    "from services.audit_service import append_audit_entry, list_audit_entries\n"
    "from services.database_service import open_database\n"
    "from services.event_service import append_event, list_events\n",
    1,
)

start = source.index("def database():\n")
end = source.index("def analyze_system", start)
replacement = '''def database():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def record_metrics(metrics):
    store_metrics(metrics, database)


def metric_history(hours=24):
    return load_metric_history(hours, database)


def write_audit(action, target, success, message=""):
    append_audit_entry(
        action,
        target,
        success,
        message,
        current_user() or "unknown",
        database,
    )


def audit_history(limit=50):
    return list_audit_entries(limit, database)


def write_event(event_key, severity, title, message):
    return append_event(
        event_key,
        severity,
        title,
        message,
        database,
    )


def event_history(limit=50):
    return list_events(limit, database)


'''
source = source[:start] + replacement + source[end:]
APP_PATH.write_text(source)
