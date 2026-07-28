from html import escape

from flask import Blueprint, current_app, jsonify, request, send_file

from services.file_browser_service import (
    FileNotAllowedError,
    FileTooLargeError,
    configured_roots,
    downloadable_file,
    list_directory,
)

file_browser_blueprint = Blueprint("file_browser", __name__)


def roots():
    return configured_roots(current_app.config["FILE_BROWSER_ROOTS"])


def selected_root(index):
    available = roots()
    if index < 0 or index >= len(available):
        raise FileNotFoundError("Ukendt filområde.")
    return available[index]


@file_browser_blueprint.get("/api/files")
def api_file_roots():
    available = roots()
    return jsonify(
        {
            "read_only": True,
            "roots": [
                {"id": index, "name": root.name}
                for index, root in enumerate(available)
            ],
            "count": len(available),
        }
    )


@file_browser_blueprint.get("/api/files/<int:root_id>")
def api_file_listing(root_id):
    try:
        limit = request.args.get("limit", default=200, type=int)
        return jsonify(
            list_directory(
                selected_root(root_id),
                request.args.get("path", ""),
                limit=limit,
            )
        )
    except FileNotFoundError:
        return jsonify({"error": "Stien blev ikke fundet."}), 404
    except (FileNotAllowedError, NotADirectoryError) as exc:
        return jsonify({"error": str(exc)}), 400


@file_browser_blueprint.get("/api/files/<int:root_id>/download")
def api_file_download(root_id):
    try:
        path = downloadable_file(
            selected_root(root_id),
            request.args.get("path", ""),
            max_bytes=current_app.config["FILE_BROWSER_MAX_DOWNLOAD_BYTES"],
        )
        return send_file(path, as_attachment=True, download_name=path.name)
    except FileNotFoundError:
        return jsonify({"error": "Filen blev ikke fundet."}), 404
    except (FileNotAllowedError, FileTooLargeError) as exc:
        return jsonify({"error": str(exc)}), 400


def _card():
    available = roots()
    root_links = "".join(
        f'<a class="btn" href="/api/files/{index}">{escape(root.name)}</a> '
        for index, root in enumerate(available)
    )
    if not root_links:
        root_links = '<span class="muted">Ingen godkendte mapper er tilgængelige.</span>'
    return (
        '<article class="card" id="files">'
        '<div class="section"><div><h2>File Browser</h2>'
        '<small>Sikker visning og download fra godkendte mapper</small></div>'
        '<span class="pill readonly">Read-only</span></div>'
        '<div class="notification-stats">'
        f'<div class="notification-stat"><span class="label">Filområder</span><strong>{len(available)}</strong></div>'
        '<div class="notification-stat"><span class="label">Upload</span><strong>Blokeret</strong></div>'
        '<div class="notification-stat"><span class="label">Sletning</span><strong>Blokeret</strong></div>'
        '</div><p>' + root_links + '<a class="btn" href="/api/files">Vis JSON</a></p></article>'
    )


def init_file_browser(app):
    app.register_blueprint(file_browser_blueprint)

    @app.after_request
    def expose_file_browser(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            payload["file_browser"] = {
                "read_only": True,
                "roots": len(roots()),
                "max_download_bytes": current_app.config[
                    "FILE_BROWSER_MAX_DOWNLOAD_BYTES"
                ],
            }
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            response.set_data(html.replace(marker, _card() + marker, 1))
            response.content_length = len(response.get_data())
        return response
