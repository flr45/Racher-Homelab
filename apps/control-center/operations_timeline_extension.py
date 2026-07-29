from flask import Blueprint, Response, current_app, jsonify, request

from rbac_extension import current_identity
from services.database_service import open_database
from services.operations_timeline_service import (
    ALLOWED_SEVERITIES,
    ALLOWED_SOURCES,
    list_timeline,
    timeline_csv,
    timeline_summary,
)
from services.rbac_service import has_permission

operations_timeline_blueprint = Blueprint("operations_timeline", __name__)


def _database_factory():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


def _csv_values(name):
    return {
        value.strip().lower()
        for value in request.args.get(name, "").split(",")
        if value.strip()
    }


def _request_options():
    try:
        limit = int(request.args.get("limit", "100"))
        since_hours = int(request.args.get("since_hours", "168"))
    except ValueError as exc:
        raise ValueError("limit og since_hours skal være heltal") from exc
    sources = _csv_values("source")
    severities = _csv_values("severity")
    invalid_sources = sources - ALLOWED_SOURCES
    invalid_severities = severities - ALLOWED_SEVERITIES
    if invalid_sources or invalid_severities:
        raise ValueError("Et eller flere filtre er ugyldige")
    return {
        "sources": sources or None,
        "severities": severities or None,
        "query": request.args.get("q", ""),
        "limit": limit,
        "since_hours": since_hours,
    }


def _require_read_permission():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify(
            {
                "error": "Brugeren har ikke tilladelse til tidslinjen.",
                "required_permission": "system.read",
            }
        ), 403
    return None


@operations_timeline_blueprint.get("/api/operations-timeline")
def api_operations_timeline():
    denied = _require_read_permission()
    if denied:
        return denied
    try:
        options = _request_options()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    items = list_timeline(_database_factory, **options)
    return jsonify(
        {
            "items": items,
            "summary": timeline_summary(items),
            "filters": {
                "sources": sorted(ALLOWED_SOURCES),
                "severities": sorted(ALLOWED_SEVERITIES),
                "max_limit": 500,
                "max_since_hours": 24 * 365,
            },
        }
    )


@operations_timeline_blueprint.get("/api/operations-timeline/export.csv")
def export_operations_timeline():
    denied = _require_read_permission()
    if denied:
        return denied
    try:
        options = _request_options()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    options["limit"] = min(options["limit"], 500)
    items = list_timeline(_database_factory, **options)
    return Response(
        timeline_csv(items),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=racher-operations-timeline.csv",
            "Cache-Control": "no-store",
        },
    )


def init_operations_timeline(app):
    app.register_blueprint(operations_timeline_blueprint)
