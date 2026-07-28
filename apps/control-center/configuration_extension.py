from flask import Blueprint, current_app, jsonify, request

from services.configuration_service import configuration_inventory, configuration_summary

configuration_blueprint = Blueprint("configuration_center", __name__)


def _payload():
    return {
        "configuration_center": configuration_summary(),
        "variables": configuration_inventory(),
    }


@configuration_blueprint.get("/api/configuration")
def api_configuration():
    return jsonify(_payload())


def _card(summary):
    health_class = "admin" if summary["healthy"] else "readonly"
    health_text = "Komplet" if summary["healthy"] else "Kræver handling"
    return (
        '<article class="card" id="configuration">'
        '<div class="section"><div><h2>Secrets & Environment</h2>'
        '<small>Read-only konfigurationskontrol uden visning af værdier</small></div>'
        f'<span class="pill {health_class}">{health_text}</span></div>'
        '<div class="notification-stats">'
        f'<div class="notification-stat"><span class="label">Konfigureret</span><strong>{summary["configured"]}/{summary["total"]}</strong></div>'
        f'<div class="notification-stat"><span class="label">Secrets</span><strong>{summary["configured_secrets"]}/{summary["secrets"]}</strong></div>'
        f'<div class="notification-stat"><span class="label">Mangler</span><strong>{summary["missing_required"]}</strong></div>'
        '</div><p><a class="btn" href="/api/configuration">Vis inventory som JSON</a></p></article>'
    )


def init_configuration_center(app):
    app.register_blueprint(configuration_blueprint)

    @app.after_request
    def expose_configuration(response):
        payload = _payload()
        summary = payload["configuration_center"]
        if request.path == "/api/status" and response.is_json:
            body = response.get_json(silent=True) or {}
            body["configuration_center"] = summary
            response.set_data(current_app.json.dumps(body))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            if marker in html:
                html = html.replace(marker, _card(summary) + marker, 1)
                response.set_data(html)
                response.content_length = len(response.get_data())
        return response
