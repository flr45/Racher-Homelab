from __future__ import annotations

import sqlite3
from typing import Callable

from flask import g, jsonify, request

from ric_noise_filter import RicNoiseFilter


def register_adaptive_routes(app, storage, routing, adaptive, auth_required: Callable) -> None:
    ric_noise = RicNoiseFilter(adaptive.db_path)

    @app.get("/api/adaptive/status")
    @auth_required(admin=True)
    def api_adaptive_status():
        stats = adaptive.stats()
        stats["ric_noise_filters"] = len(ric_noise.list_filters())
        return jsonify({
            "stats": stats,
            "station_suggestions": routing.list_station_suggestions(limit=40),
            "ric_noise_filters": ric_noise.list_filters(),
        })

    @app.get("/api/adaptive/review")
    @auth_required(admin=True)
    def api_adaptive_review():
        # Fetch more rows than the UI needs because known diagnostic RICs are
        # removed after retrieval. This lets older useful messages fill the queue
        # instead of a burst of filtered diagnostics leaving it almost empty.
        rows = adaptive.review_queue(limit=200)
        return jsonify(ric_noise.filter_review_rows(rows, limit=60))

    @app.get("/api/adaptive/ric-filters")
    @auth_required(admin=True)
    def api_adaptive_ric_filters_get():
        return jsonify(ric_noise.list_filters())

    @app.post("/api/adaptive/ric-filters")
    @auth_required(admin=True)
    def api_adaptive_ric_filters_post():
        body = request.get_json(silent=True) or {}
        try:
            row = ric_noise.add(body.get("ric"), body.get("label", ""), int(g.user["id"]))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "adaptive-ric-filter-add",
            f"ric={row['ric']}; label={row['label'] or '-'}",
        )
        return jsonify({"ok": True, "filter": row})

    @app.delete("/api/adaptive/ric-filters/<ric>")
    @auth_required(admin=True)
    def api_adaptive_ric_filters_delete(ric: str):
        try:
            removed = ric_noise.remove(ric)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not removed:
            return jsonify({"ok": False, "error": "RIC-filteret findes ikke."}), 404
        storage.add_audit(g.user["id"], "adaptive-ric-filter-delete", f"ric={ric}")
        return jsonify({"ok": True})

    @app.post("/api/adaptive/messages/<int:message_id>/feedback")
    @auth_required(admin=True)
    def api_adaptive_feedback(message_id: int):
        body = request.get_json(silent=True) or {}
        verdict = str(body.get("verdict") or "").strip().lower()
        try:
            learned = adaptive.record_feedback(message_id, verdict, int(g.user["id"]))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "adaptive-feedback",
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
        storage.add_audit(g.user["id"], "station-create", f"station={row['key']}; name={row['name']}")
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
        storage.add_audit(g.user["id"], "station-update", f"station={station_key}; name={row['name']}")
        return jsonify({"ok": True, "station": row})
