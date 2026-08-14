from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from gateway import FileTailSource, PagerEvent, PushoverClient, detect_station, parse_pdl_line
from storage import Storage


DATA_DIR = Path(os.getenv("PAGER_DATA_DIR", "/data"))
DB_PATH = os.getenv("PAGER_DB_PATH", str(DATA_DIR / "pager.db"))

app = Flask(__name__)
storage = Storage(DB_PATH)
pushover = PushoverClient()
started_at = datetime.now(timezone.utc)


def setting(name: str, default: str = "") -> str:
    return storage.get_settings().get(name, default)


def station_is_enabled(station: str | None) -> bool:
    if not station:
        return True
    keys = {
        "Slagelse": "station_a_enabled",
        "Sorø": "station_s_enabled",
        "Korsør": "station_k_enabled",
        "Skælskør": "station_l_enabled",
        "Ruds Vedby": "station_r_enabled",
    }
    key = keys.get(station)
    return True if not key else setting(key, "1") == "1"


def maybe_notify(message_id: int, event: dict) -> None:
    settings = storage.get_settings()
    if settings.get("pushover_enabled") != "1":
        return
    if not station_is_enabled(event.get("station")):
        return
    title = event.get("station") or settings.get("gateway_name", "Pager")
    pushover.send(
        settings.get("pushover_app_token", ""),
        settings.get("pushover_user_key", ""),
        title,
        event.get("message", ""),
    )
    storage.mark_notification_sent(message_id)


def ingest_event(event: PagerEvent) -> int:
    data = event.to_dict()
    message_id = storage.add_message(data)
    try:
        maybe_notify(message_id, data)
    except Exception as exc:
        app.logger.warning("Pushover failed for message %s: %s", message_id, exc)
    return message_id


def on_pdl_line(line: str) -> None:
    event = parse_pdl_line(line, source="pdl-file")
    if event:
        ingest_event(event)


source = FileTailSource(lambda: setting("pdl_log_path", "/data/pdl.log"), on_pdl_line)
source.start()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    latest = storage.latest_message()
    now = datetime.now(timezone.utc)
    return jsonify(
        {
            "name": setting("gateway_name", "Racher Pager Gateway"),
            "hostname": socket.gethostname(),
            "source_mode": setting("source_mode", "mock"),
            "source": source.status,
            "message_count": storage.message_count(),
            "latest_message": latest,
            "uptime_seconds": int((now - started_at).total_seconds()),
            "server_time": now.isoformat(),
        }
    )


@app.get("/api/messages")
def api_messages():
    limit = request.args.get("limit", "100")
    try:
        limit_int = int(limit)
    except ValueError:
        limit_int = 100
    return jsonify(storage.list_messages(limit_int))


@app.get("/api/settings")
def api_settings_get():
    settings = storage.get_settings()
    settings["pushover_app_token_set"] = bool(settings.get("pushover_app_token"))
    settings["pushover_user_key_set"] = bool(settings.get("pushover_user_key"))
    settings["pushover_app_token"] = ""
    settings["pushover_user_key"] = ""
    return jsonify(settings)


@app.post("/api/settings")
def api_settings_post():
    payload = request.get_json(silent=True) or {}
    current = storage.get_settings()

    for secret in ("pushover_app_token", "pushover_user_key"):
        if secret in payload and not str(payload[secret]).strip():
            payload[secret] = current.get(secret, "")

    allowed_bool = {
        "station_a_enabled",
        "station_s_enabled",
        "station_k_enabled",
        "station_l_enabled",
        "station_r_enabled",
        "pushover_enabled",
    }
    for key in allowed_bool:
        if key in payload:
            payload[key] = "1" if str(payload[key]).lower() in {"1", "true", "on", "yes"} else "0"

    storage.update_settings(payload)
    return jsonify({"ok": True})


@app.post("/api/mock")
def api_mock():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("message") or "(A) TESTALARM - Racher Pager Gateway").strip()
    if not text:
        return jsonify({"ok": False, "error": "message is required"}), 400

    raw_line = str(payload.get("raw_line") or text)
    event = parse_pdl_line(raw_line, source="mock") or PagerEvent(
        message=text,
        raw_line=raw_line,
        source="mock",
    )
    event.message = text
    event.station = detect_station(text)
    message_id = ingest_event(event)
    return jsonify({"ok": True, "id": message_id})


@app.post("/api/pushover/test")
def api_pushover_test():
    settings = storage.get_settings()
    try:
        pushover.send(
            settings.get("pushover_app_token", ""),
            settings.get("pushover_user_key", ""),
            "Racher Pager Gateway",
            "Testbesked fra pager-gatewayen.",
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")), debug=False)
