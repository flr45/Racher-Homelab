from __future__ import annotations

import sqlite3
from typing import Any, Callable

from flask import jsonify, request


def register_adaptive_routes(app, storage, routing, adaptive, auth_required: Callable) -> None:
    @app.get("/api/adaptive/status")
    @auth_required(admin=True)
    def api_adaptive_status():
        return jsonify({
            "stats": adaptive.stats(),
            "station_suggestions": routing.list_station_suggestions(limit=40),
        })

    @app.get("/api/adaptive/review")
    @auth_required(admin=True)
    def api_adaptive_review():
        return jsonify(adaptive.review_queue(limit=60))

    @app.post("/api/adaptive/messages/<int:message_id>/feedback")
    @auth_required(admin=True)
    def api_adaptive_feedback(message_id: int):
        body = request.get_json(silent=True) or {}
        verdict = str(body.get("verdict") or "").strip().lower()
        try:
            learned = adaptive.record_feedback(message_id, verdict, request.environ["racher.user_id"])
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            request.environ["racher.user_id"],
            "adaptive-feedback",
            f"message_id={message_id}; verdict={verdict}; learned={learned.get('classification')}",
        )
        return jsonify({"ok": True, "learned": learned})

    @app.post("/api/stations")
    @auth_required(admin=True)
    def api_station_create():
        body = request.get_json(silent=True) or {}
        try:
            row = routing.create_station(body.get("name"), source="admin")
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(request.environ["racher.user_id"], "station-create", f"station={row['key']}; name={row['name']}")
        return jsonify({"ok": True, "station": row})

    @app.patch("/api/stations/<station_key>")
    @auth_required(admin=True)
    def api_station_update(station_key: str):
        body = request.get_json(silent=True) or {}
        try:
            row = routing.update_station(
                station_key,
                name=body.get("name") if "name" in body else None,
                active=body.get("active") if "active" in body else None,
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not row:
            return jsonify({"ok": False, "error": "Stationen findes ikke."}), 404
        storage.add_audit(request.environ["racher.user_id"], "station-update", f"station={station_key}; name={row['name']}")
        return jsonify({"ok": True, "station": row})
