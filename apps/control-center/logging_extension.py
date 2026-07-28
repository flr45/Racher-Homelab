import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from flask import g, request

_REDACT_PATTERN = re.compile(
    r"(?i)(authorization|token|secret|password|api[_-]?key|client[_-]?secret)(\s*[:=]\s*)[^\s,;]+"
)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for field in (
            "event",
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "remote_addr",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def redact_text(value):
    return _REDACT_PATTERN.sub(r"\1\2[REDACTED]", str(value or ""))


def configure_logging(app):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(app.config.get("LOG_LEVEL", "INFO"))
    app.logger.propagate = False


def init_structured_logging(app):
    configure_logging(app)

    @app.before_request
    def start_request_log():
        supplied = request.headers.get("X-Request-ID", "").strip()
        g.request_id = supplied[:128] if supplied else uuid.uuid4().hex
        g.request_started_at = time.monotonic()

    @app.after_request
    def finish_request_log(response):
        request_id = getattr(g, "request_id", uuid.uuid4().hex)
        started_at = getattr(g, "request_started_at", time.monotonic())
        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        app.logger.info(
            "request completed",
            extra={
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "path": request.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "remote_addr": request.remote_addr,
            },
        )
        return response

    @app.errorhandler(Exception)
    def log_unhandled_error(error):
        request_id = getattr(g, "request_id", uuid.uuid4().hex)
        app.logger.exception(
            "unhandled request error",
            extra={
                "event": "unhandled_error",
                "request_id": request_id,
                "method": request.method,
                "path": request.path,
            },
        )
        if app.config.get("TESTING"):
            raise error
        return {"error": "Intern serverfejl.", "request_id": request_id}, 500
