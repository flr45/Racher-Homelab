from __future__ import annotations

import threading
from typing import Any, Callable

from flask import g, jsonify, request


_TRAINING_APPLY_LOCK = threading.Lock()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().casefold() in {"1", "true", "yes", "ja", "on"}


def _duplicate_window_seconds(storage: Any) -> int:
    try:
        return max(1, min(int(storage.get_setting("duplicate_window_seconds", "30")), 300))
    except (TypeError, ValueError):
        return 30


def register_training_routes(app: Any, storage: Any, training: Any, auth_required: Callable) -> None:
    @app.get("/api/training/runs")
    @auth_required(admin=True)
    def api_training_runs():
        return jsonify(training.list_runs(limit=30))

    @app.post("/api/training/replay")
    @auth_required(admin=True)
    def api_training_replay():
        body = request.get_json(silent=True) or {}
        try:
            run = training.create_replay(
                body.get("name"),
                body.get("text"),
                int(g.user["id"]),
                _duplicate_window_seconds(storage),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "training-replay",
            f"run_id={run['id']}; lines={run['total_lines']}; parsed={run['parsed_count']}",
        )
        return jsonify({"ok": True, "run": run})

    @app.get("/api/training/runs/<int:run_id>")
    @auth_required(admin=True)
    def api_training_run(run_id: int):
        try:
            return jsonify(training.get_run(run_id, include_events=True))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.patch("/api/training/events/<int:event_id>")
    @auth_required(admin=True)
    def api_training_event_feedback(event_id: int):
        body = request.get_json(silent=True) or {}
        try:
            row = training.set_event_feedback(event_id, body.get("feedback"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "event": row})

    @app.patch("/api/training/runs/<int:run_id>/candidates")
    @auth_required(admin=True)
    def api_training_candidate_decisions(run_id: int):
        body = request.get_json(silent=True) or {}
        try:
            run = training.set_candidate_decisions(
                run_id, body.get("stations"), body.get("rics"),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "run": run})

    @app.post("/api/training/runs/<int:run_id>/apply")
    @auth_required(admin=True)
    def api_training_apply(run_id: int):
        # Applying a run mutates several learning/routing tables before the run is
        # finally marked as applied. Gunicorn deliberately runs one web worker for
        # this appliance, but requests are threaded; two near-simultaneous apply
        # requests could otherwise both pass the applied_at check and count the
        # same feedback twice. Serialize this critical section at the HTTP edge.
        with _TRAINING_APPLY_LOCK:
            try:
                result = training.apply_run(run_id)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "training-apply",
            (
                f"run_id={run_id}; stations={result['stations_created']}; "
                f"rics={result['rics_created']}; feedback={result['feedback_applied']}"
            ),
        )
        return jsonify({"ok": True, "result": result})

    @app.post("/api/training/ric-import/preview")
    @auth_required(admin=True)
    def api_training_ric_import_preview():
        body = request.get_json(silent=True) or {}
        try:
            preview = training.preview_ric_import(body.get("text"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "preview": preview})

    @app.post("/api/training/ric-import/apply")
    @auth_required(admin=True)
    def api_training_ric_import_apply():
        body = request.get_json(silent=True) or {}
        create_missing = _as_bool(body.get("create_missing_stations"), True)
        try:
            result = training.apply_ric_import(
                body.get("text"), create_missing, int(g.user["id"]),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "ric-bulk-import",
            (
                f"created={result['created']}; skipped={result['skipped_existing']}; "
                f"stations={result['stations_created']}; errors={len(result['errors'])}"
            ),
        )
        return jsonify({"ok": True, "result": result})
