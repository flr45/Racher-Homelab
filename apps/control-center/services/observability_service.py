import re

from services.metrics_service import metric_history

_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REDACTIONS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s]+"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|client[_-]?secret)(\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+@"),
)


def validate_container_name(name):
    value = str(name or "").strip()
    if not _CONTAINER_NAME.fullmatch(value):
        raise ValueError("Ugyldigt containernavn.")
    return value


def redact_log_line(line):
    value = str(line or "")
    value = _REDACTIONS[0].sub(r"\1[REDACTED]", value)
    value = _REDACTIONS[1].sub(r"\1\2[REDACTED]", value)
    return _REDACTIONS[2].sub(r"\1[REDACTED]@", value)


def normalize_logs(raw_logs, *, query="", limit=500):
    needle = str(query or "").strip().casefold()[:200]
    result = []
    for raw_line in str(raw_logs or "").splitlines():
        line = redact_log_line(raw_line)
        if needle and needle not in line.casefold():
            continue
        timestamp, separator, message = line.partition(" ")
        if not separator or "T" not in timestamp:
            timestamp, message = None, line
        result.append({"timestamp": timestamp, "message": message})
    return result[-max(1, min(int(limit), 500)) :]


def history_snapshot(hours, database_factory):
    bounded_hours = max(1, min(int(hours), 24 * 30))
    points = metric_history(bounded_hours, database_factory)
    return {"hours": bounded_hours, "points": points, "count": len(points)}


def log_snapshot(container_name, *, tail, query, logs_loader):
    safe_name = validate_container_name(container_name)
    bounded_tail = max(1, min(int(tail), 500))
    resolved_name, raw_logs = logs_loader(safe_name, bounded_tail)
    entries = normalize_logs(raw_logs, query=query, limit=bounded_tail)
    return {
        "container": resolved_name,
        "tail": bounded_tail,
        "query": str(query or "")[:200],
        "entries": entries,
        "count": len(entries),
    }
