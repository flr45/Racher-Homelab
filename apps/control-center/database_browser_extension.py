from html import escape

from flask import Blueprint, current_app, jsonify, request

from services.database_browser_service import (
    DatabaseObjectNotFoundError,
    list_tables,
    table_details,
    table_rows,
)
from services.database_service import open_database

database_browser_blueprint = Blueprint("database_browser", __name__)


def database():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


@database_browser_blueprint.get("/api/database")
def api_database():
    return jsonify({"read_only": True, "tables": list_tables(database)})


@database_browser_blueprint.get("/api/database/<table_name>")
def api_database_table(table_name):
    try:
        return jsonify({"read_only": True, "table": table_details(table_name, database)})
    except DatabaseObjectNotFoundError:
        return jsonify({"error": "Tabellen blev ikke fundet."}), 404


@database_browser_blueprint.get("/api/database/<table_name>/rows")
def api_database_rows(table_name):
    page = request.args.get("page", default=1, type=int)
    page_size = request.args.get("page_size", default=50, type=int)
    try:
        return jsonify(
            {
                "read_only": True,
                **table_rows(
                    table_name,
                    database,
                    page=page,
                    page_size=page_size,
                ),
            }
        )
    except DatabaseObjectNotFoundError:
        return jsonify({"error": "Tabellen blev ikke fundet."}), 404


def _card():
    try:
        tables = list_tables(database)
        total_rows = sum(item["rows"] for item in tables)
        preview = "".join(
            f'<div class="event"><strong>{escape(item["name"])}</strong>'
            f'<small>{item["rows"]} rækker</small></div>'
            for item in tables[:5]
        )
        body = (
            '<div class="notification-stats">'
            f'<div class="notification-stat"><span class="label">Tabeller</span><strong>{len(tables)}</strong></div>'
            f'<div class="notification-stat"><span class="label">Rækker</span><strong>{total_rows}</strong></div>'
            '<div class="notification-stat"><span class="label">Adgang</span><strong>Read-only</strong></div>'
            '</div>'
            f'{preview}'
            '<p><a class="btn" href="/api/database">Vis database</a></p>'
        )
    except Exception:
        body = '<div class="bad">Databasemetadata kunne ikke indlæses.</div>'

    return (
        '<article class="card" id="database-browser">'
        '<div class="section"><div><h2>Database Browser</h2>'
        '<small>SQLite-tabeller, struktur, indeks og redigerede rækker</small></div>'
        '<span class="pill readonly">Read-only</span></div>'
        f'{body}</article>'
    )


def init_database_browser(app):
    app.register_blueprint(database_browser_blueprint)

    @app.after_request
    def expose_database_browser(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            try:
                tables = list_tables(database)
                payload["database_browser"] = {
                    "healthy": True,
                    "read_only": True,
                    "tables": len(tables),
                    "rows": sum(item["rows"] for item in tables),
                }
            except Exception:
                payload["database_browser"] = {
                    "healthy": False,
                    "read_only": True,
                    "tables": 0,
                    "rows": 0,
                }
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            response.set_data(html.replace(marker, _card() + marker, 1))
            response.content_length = len(response.get_data())
        return response
