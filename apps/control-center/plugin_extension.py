from flask import Blueprint, current_app, jsonify, request

from services.plugin_service import PluginManifestError, discover_plugins

plugin_blueprint = Blueprint("plugins", __name__)


def snapshot():
    return discover_plugins(
        current_app.config["PLUGIN_ROOT"],
        platform_version=current_app.config["RACHER_OS_VERSION"],
    )


@plugin_blueprint.get("/api/plugins")
def api_plugins():
    try:
        return jsonify(snapshot())
    except PluginManifestError:
        return jsonify({"error": "Plugin inventory kunne ikke indlæses."}), 503


def _card():
    return (
        '<article class="card" id="plugins">'
        '<div class="section"><div><h2>Plugin Center</h2>'
        '<small>Deklarative manifests uden kodekørsel</small></div>'
        '<span class="pill readonly">Read-only</span></div>'
        '<div class="notification-stats">'
        '<div class="notification-stat"><span class="label">Plugins</span><strong id="plugin-count">–</strong></div>'
        '<div class="notification-stat"><span class="label">Kompatible</span><strong id="plugin-compatible">–</strong></div>'
        '<div class="notification-stat"><span class="label">Execution</span><strong>Deaktiveret</strong></div>'
        '</div><p><a class="btn" href="/api/plugins">Vis plugin inventory</a></p>'
        '<script>(async()=>{try{const r=await fetch("/api/plugins");const d=await r.json();'
        'document.getElementById("plugin-count").textContent=d.count??0;'
        'document.getElementById("plugin-compatible").textContent=d.compatible??0;'
        '}catch(e){document.getElementById("plugin-count").textContent="Fejl"}})();</script></article>'
    )


def init_plugin_center(app):
    app.register_blueprint(plugin_blueprint)

    @app.after_request
    def expose_plugins(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            try:
                data = snapshot()
                payload["plugins"] = {
                    "count": data.get("count", 0),
                    "compatible": data.get("compatible", 0),
                    "invalid": len(data.get("invalid", [])),
                    "execution_enabled": False,
                }
            except PluginManifestError:
                payload["plugins"] = {"available": False, "execution_enabled": False}
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            response.set_data(html.replace(marker, _card() + marker, 1))
            response.content_length = len(response.get_data())
        return response
