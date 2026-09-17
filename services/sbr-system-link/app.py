from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
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


def legacy_shared_secret() -> str:
    return (os.environ.get("SYSTEM_LINK_SECRET") or "").strip()


def master_secret() -> str:
    return (os.environ.get("SYSTEM_LINK_MASTER_SECRET") or "").strip()


def provision_token() -> str:
    return (os.environ.get("SYSTEM_LINK_PROVISION_TOKEN") or "").strip()


def public_endpoint() -> str:
    return (os.environ.get("SYSTEM_LINK_PUBLIC_ENDPOINT") or "").strip()


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


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def init_database() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS installations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL UNIQUE,
                machine_name TEXT,
                app_version TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS provision_tokens (
                token_hash TEXT PRIMARY KEY,
                consumed_at TEXT NOT NULL,
                client_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                client_id TEXT,
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
        if "client_id" not in _columns(db, "deliveries"):
            db.execute("ALTER TABLE deliveries ADD COLUMN client_id TEXT")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_deliveries_client "
            "ON deliveries(client_id, receiver_received_at DESC)"
        )


def _derive_client_secret(client_id: str) -> str:
    secret = master_secret()
    if not secret:
        raise RuntimeError("SYSTEM_LINK_MASTER_SECRET mangler")
    return hmac.new(
        secret.encode("utf-8"),
        f"client:{client_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _signature_valid(raw_body: bytes) -> tuple[bool, str | None]:
    supplied = (request.headers.get("X-System-Link-Signature") or "").strip()
    client_id = (request.headers.get("X-System-Link-Client") or "").strip()

    if client_id:
        with connection() as db:
            installation = db.execute(
                "SELECT client_id, revoked FROM installations WHERE client_id=?",
                (client_id,),
            ).fetchone()
        if not installation or int(installation["revoked"] or 0):
            return False, client_id
        try:
            secret = _derive_client_secret(client_id)
        except RuntimeError:
            return False, client_id
    else:
        secret = legacy_shared_secret()
        if not secret:
            return False, None

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied, expected), client_id or None


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


def _store_payload(payload: dict, raw_body: bytes, client_id: str | None) -> bool:
    message = payload["message"]
    event = payload.get("event") or {}
    delivery_id = str(payload["deliveryId"])
    raw_json = raw_body.decode("utf-8")

    with connection() as db:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO deliveries(
                delivery_id, client_id, receiver_received_at, source_ip, schema_name,
                gateway, source_id, sender, body, message_received_at,
                message_created_at, modem_port, processing_status,
                event_id, station, alarm_type, address, event_status,
                followup_count, event_started_at, event_completed_at, raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                delivery_id,
                client_id,
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
        if client_id:
            db.execute(
                "UPDATE installations SET last_seen_at=? WHERE client_id=?",
                (utcnow_iso(), client_id),
            )
        return cursor.rowcount == 1


@app.get("/health")
def health():
    if not (master_secret() or legacy_shared_secret()):
        return jsonify(status="error", detail="System Link-nøgle mangler"), 503
    try:
        init_database()
    except sqlite3.Error as exc:
        return jsonify(status="error", detail=f"database: {exc}"), 503
    return jsonify(
        status="ok",
        service="sbr-system-link",
        provisioning=bool(master_secret() and provision_token()),
    )


@app.post("/api/provision")
def provision_client():
    if not master_secret() or not provision_token():
        return jsonify(ok=False, error="provisionering er ikke konfigureret"), 503

    raw_body = request.get_data(cache=False)
    if not raw_body or len(raw_body) > 32 * 1024:
        return jsonify(ok=False, error="ugyldig request"), 400
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(ok=False, error="ugyldig JSON"), 400
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="ugyldig payload"), 400

    supplied_token = str(payload.get("token") or "")
    expected_token = provision_token()
    if not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify(ok=False, error="ugyldig provisioneringskode"), 401

    token_hash = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
    machine_name = str(payload.get("machineName") or "")[:200]
    app_version = str(payload.get("appVersion") or "")[:100]
    client_id = "gw-" + secrets.token_hex(12)
    client_secret = _derive_client_secret(client_id)
    now = utcnow_iso()

    try:
        with connection() as db:
            db.execute(
                """
                INSERT INTO provision_tokens(token_hash, consumed_at, client_id)
                VALUES(?,?,?)
                """,
                (token_hash, now, client_id),
            )
            db.execute(
                """
                INSERT INTO installations(client_id, machine_name, app_version, created_at)
                VALUES(?,?,?,?)
                """,
                (client_id, machine_name, app_version, now),
            )
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error="provisioneringskoden er allerede brugt"), 409

    return jsonify(
        ok=True,
        clientId=client_id,
        clientSecret=client_secret,
        messageEndpoint=public_endpoint(),
        receiverTime=now,
    ), 201


@app.post("/api/system-link")
def receive_system_link():
    if not (master_secret() or legacy_shared_secret()):
        return jsonify(ok=False, error="receiver er ikke konfigureret"), 503

    raw_body = request.get_data(cache=False)
    if not raw_body:
        return jsonify(ok=False, error="tom request"), 400
    if len(raw_body) > MAX_BODY_BYTES:
        return jsonify(ok=False, error="request er for stor"), 413

    signature_ok, client_id = _signature_valid(raw_body)
    if not signature_ok:
        return jsonify(ok=False, error="ugyldig signatur"), 401

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(ok=False, error="ugyldig JSON"), 400

    payload, error = _validate_payload(payload)
    if error:
        return jsonify(ok=False, error=error), 400
    assert payload is not None

    if client_id:
        with connection() as db:
            db.execute(
                "UPDATE installations SET last_seen_at=? WHERE client_id=?",
                (utcnow_iso(), client_id),
            )

    if payload.get("test") is True:
        return jsonify(ok=True, test=True, clientId=client_id, receiverTime=utcnow_iso())

    init_database()
    inserted = _store_payload(payload, raw_body, client_id)
    return (
        jsonify(
            ok=True,
            duplicate=not inserted,
            deliveryId=payload["deliveryId"],
            clientId=client_id,
            receiverTime=utcnow_iso(),
        ),
        201 if inserted else 200,
    )


if __name__ == "__main__":
    init_database()
    app.run(host="0.0.0.0", port=8098)
