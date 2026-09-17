from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

SCHEMA = "sbr-pager-gateway.system-link.v1"
MAX_BODY_BYTES = 256 * 1024

app = Flask(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def db_path() -> Path:
    return Path(os.environ.get("SYSTEM_LINK_DB", "/data/system-link.db"))


def shared_secret() -> str:
    return (os.environ.get("SYSTEM_LINK_SECRET") or "").strip()


@contextmanager
def connection():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_database() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                receiver_received_at TEXT NOT NULL,
                source_ip TEXT,
                schema_name TEXT NOT NULL,
                gateway TEXT,
                source_id TEXT,
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                message_received_at TEXT,
                message_created_at TEXT,
                modem_port TEXT,
                processing_status TEXT,
                event_id TEXT,
                station TEXT,
                alarm_type TEXT,
                address TEXT,
                event_status TEXT,
                followup_count INTEGER NOT NULL DEFAULT 0,
                event_started_at TEXT,
                event_completed_at TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_deliveries_received
                ON deliveries(receiver_received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_deliveries_station
                ON deliveries(station, receiver_received_at DESC);
            """
        )


def _signature_valid(raw_body: bytes) -> bool:
    secret = shared_secret()
    if not secret:
        return False
    supplied = (request.headers.get("X-System-Link-Signature") or "").strip()
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied, expected)


def _source_ip() -> str | None:
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
    )


def _validate_payload(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "JSON payload skal være et objekt"
    if payload.get("schema") != SCHEMA:
        return None, "Ukendt System Link schema"
    if payload.get("test") is True:
        return payload, None

    delivery_id = str(payload.get("deliveryId") or "").strip()
    if not delivery_id or len(delivery_id) > 200:
        return None, "deliveryId mangler eller er ugyldig"

    message = payload.get("message")
    if not isinstance(message, dict):
        return None, "message mangler"
    sender = str(message.get("sender") or "").strip()
    body = str(message.get("body") or "")
    if not sender or not body:
        return None, "message.sender og message.body er påkrævet"
    if len(body) > 100_000:
        return None, "message.body er for stor"

    event = payload.get("event")
    if event is not None and not isinstance(event, dict):
        return None, "event skal være et objekt eller null"
    return payload, None


def _store_payload(payload: dict, raw_body: bytes) -> bool:
    message = payload["message"]
    event = payload.get("event") or {}
    delivery_id = str(payload["deliveryId"])
    raw_json = raw_body.decode("utf-8")

    with connection() as db:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO deliveries(
                delivery_id, receiver_received_at, source_ip, schema_name,
                gateway, source_id, sender, body, message_received_at,
                message_created_at, modem_port, processing_status,
                event_id, station, alarm_type, address, event_status,
                followup_count, event_started_at, event_completed_at, raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                delivery_id,
                utcnow_iso(),
                _source_ip(),
                str(payload.get("schema") or ""),
                str(payload.get("gateway") or ""),
                str(message.get("sourceId") or ""),
                str(message.get("sender") or ""),
                str(message.get("body") or ""),
                message.get("receivedAt"),
                message.get("createdAt"),
                message.get("modemPort"),
                message.get("processingStatus"),
                str(event.get("id") or "") or None,
                event.get("station"),
                event.get("alarmType"),
                event.get("address"),
                event.get("status"),
                int(event.get("followupCount") or 0),
                event.get("startedAt"),
                event.get("completedAt"),
                raw_json,
            ),
        )
        return cursor.rowcount == 1


@app.get("/health")
def health():
    if not shared_secret():
        return jsonify(status="error", detail="SYSTEM_LINK_SECRET mangler"), 503
    try:
        init_database()
    except sqlite3.Error as exc:
        return jsonify(status="error", detail=f"database: {exc}"), 503
    return jsonify(status="ok", service="sbr-system-link")


@app.post("/api/system-link")
def receive_system_link():
    if not shared_secret():
        return jsonify(ok=False, error="receiver er ikke konfigureret"), 503

    raw_body = request.get_data(cache=False)
    if not raw_body:
        return jsonify(ok=False, error="tom request"), 400
    if len(raw_body) > MAX_BODY_BYTES:
        return jsonify(ok=False, error="request er for stor"), 413
    if not _signature_valid(raw_body):
        return jsonify(ok=False, error="ugyldig signatur"), 401

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(ok=False, error="ugyldig JSON"), 400

    payload, error = _validate_payload(payload)
    if error:
        return jsonify(ok=False, error=error), 400
    assert payload is not None

    if payload.get("test") is True:
        return jsonify(ok=True, test=True, receiverTime=utcnow_iso())

    init_database()
    inserted = _store_payload(payload, raw_body)
    return (
        jsonify(
            ok=True,
            duplicate=not inserted,
            deliveryId=payload["deliveryId"],
            receiverTime=utcnow_iso(),
        ),
        201 if inserted else 200,
    )


if __name__ == "__main__":
    init_database()
    app.run(host="0.0.0.0", port=8098)
