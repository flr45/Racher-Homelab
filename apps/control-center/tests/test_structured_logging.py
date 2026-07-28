import json
import logging
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from logging_extension import JsonFormatter, init_structured_logging, redact_text


def test_redact_text_hides_credentials():
    value = "token=abc123 password: hunter2 Authorization=Bearer-secret"
    redacted = redact_text(value)
    assert "abc123" not in redacted
    assert "hunter2" not in redacted
    assert "Bearer-secret" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_json_formatter_emits_structured_record():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("structured-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "request token=super-secret",
        extra={"event": "test", "request_id": "req-1", "status_code": 200},
    )
    payload = json.loads(stream.getvalue())
    assert payload["event"] == "test"
    assert payload["request_id"] == "req-1"
    assert payload["status_code"] == 200
    assert "super-secret" not in payload["message"]


def test_request_id_is_preserved_and_generated(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_structured_logging(app)
    client = app.test_client()

    supplied = client.get("/health", headers={"X-Request-ID": "external-123"})
    assert supplied.headers["X-Request-ID"] == "external-123"

    generated = client.get("/health")
    assert len(generated.headers["X-Request-ID"]) == 32


def test_request_id_header_is_bounded(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_structured_logging(app)
    response = app.test_client().get("/health", headers={"X-Request-ID": "a" * 500})
    assert response.headers["X-Request-ID"] == "a" * 128
