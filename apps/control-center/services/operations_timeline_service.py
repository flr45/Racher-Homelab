import csv
import io
from datetime import datetime, timedelta, timezone

ALLOWED_SOURCES = frozenset(
    {"audit", "event", "notification", "deployment", "rollout"}
)
ALLOWED_SEVERITIES = frozenset({"info", "warning", "critical", "success", "error"})


def utc_now():
    return datetime.now(timezone.utc)


def _clean(value, limit=1000):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _severity(value, *, success=None, status=None):
    normalized = str(value or "").strip().lower()
    if normalized in {"critical", "error", "failed", "unhealthy", "rolled_back"}:
        return "critical" if normalized == "critical" else "error"
    if normalized in {"warning", "pending", "retry", "rolling_back", "missing"}:
        return "warning"
    if normalized in {"success", "succeeded", "sent", "healthy", "running"}:
        return "success"
    if success is not None:
        return "success" if bool(success) else "error"
    if status:
        return _severity(status)
    return "info"


def _timeline_queries():
    return {
        "audit": """
            SELECT recorded_at AS timestamp, 'audit' AS source,
                   action AS title, COALESCE(message, target) AS message,
                   target AS target, actor AS actor, success AS success,
                   NULL AS raw_severity, NULL AS status
            FROM audit_log
        """,
        "event": """
            SELECT recorded_at AS timestamp, 'event' AS source,
                   title, message, event_key AS target, NULL AS actor,
                   NULL AS success, severity AS raw_severity, NULL AS status
            FROM events
        """,
        "notification": """
            SELECT created_at AS timestamp, 'notification' AS source,
                   title, message, channel AS target, NULL AS actor,
                   NULL AS success, severity AS raw_severity, status
            FROM notifications
        """,
        "deployment": """
            SELECT recorded_at AS timestamp, 'deployment' AS source,
                   change_type AS title,
                   COALESCE(image_reference, status, container_name) AS message,
                   container_name AS target, NULL AS actor,
                   NULL AS success, NULL AS raw_severity, status
            FROM deployment_history
        """,
        "rollout": """
            SELECT updated_at AS timestamp, 'rollout' AS source,
                   phase AS title, COALESCE(message, target_image) AS message,
                   container_name AS target, actor,
                   CASE WHEN status = 'succeeded' THEN 1
                        WHEN status IN ('failed', 'rolled_back') THEN 0
                        ELSE NULL END AS success,
                   NULL AS raw_severity, status
            FROM rollout_jobs
        """,
    }


def list_timeline(
    database_factory,
    *,
    sources=None,
    severities=None,
    query=None,
    since_hours=168,
    limit=100,
):
    selected_sources = set(sources or ALLOWED_SOURCES) & ALLOWED_SOURCES
    selected_severities = set(severities or ALLOWED_SEVERITIES) & ALLOWED_SEVERITIES
    limit = min(500, max(1, int(limit)))
    since_hours = min(24 * 365, max(1, int(since_hours)))
    cutoff = (utc_now() - timedelta(hours=since_hours)).isoformat()
    needle = _clean(query, 200)
    needle = needle.lower() if needle else None

    rows = []
    with database_factory() as connection:
        for source, statement in _timeline_queries().items():
            if source not in selected_sources:
                continue
            for row in connection.execute(
                f"SELECT * FROM ({statement}) WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall():
                item = dict(row)
                severity = _severity(
                    item.pop("raw_severity", None),
                    success=item.pop("success", None),
                    status=item.get("status"),
                )
                if severity not in selected_severities:
                    continue
                normalized = {
                    "timestamp": _clean(item.get("timestamp"), 100),
                    "source": source,
                    "severity": severity,
                    "title": _clean(item.get("title"), 255) or source,
                    "message": _clean(item.get("message"), 1000),
                    "target": _clean(item.get("target"), 255),
                    "actor": _clean(item.get("actor"), 320),
                    "status": _clean(item.get("status"), 100),
                }
                if needle and needle not in " ".join(
                    str(value or "").lower() for value in normalized.values()
                ):
                    continue
                rows.append(normalized)

    rows.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    return rows[:limit]


def timeline_summary(items):
    by_source = {}
    by_severity = {}
    for item in items:
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1
        by_severity[item["severity"]] = by_severity.get(item["severity"], 0) + 1
    return {
        "total": len(items),
        "by_source": dict(sorted(by_source.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "latest_at": items[0]["timestamp"] if items else None,
    }


def timeline_csv(items):
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "timestamp",
            "source",
            "severity",
            "title",
            "message",
            "target",
            "actor",
            "status",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(items)
    return output.getvalue()
