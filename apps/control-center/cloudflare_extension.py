import os
from html import escape

from flask import Blueprint, current_app, jsonify, request

from services.cloudflare_service import cloudflare_status

cloudflare_blueprint = Blueprint("cloudflare_center", __name__)


def status():
    return cloudflare_status(current_app.config)


@cloudflare_blueprint.get("/api/cloudflare")
def api_cloudflare():
    force = request.args.get("refresh", "").lower() == "true"
    return jsonify({"cloudflare": cloudflare_status(current_app.config, force=force)})


def _card(snapshot):
    if not snapshot.get("enabled"):
        body = '<div class="muted">Konfigurér Cloudflare account, zone og API-token for at aktivere modulet.</div>'
        badge = '<span class="pill readonly">Ikke konfigureret</span>'
    elif snapshot.get("error") and not snapshot.get("zone"):
        body = f'<div class="bad">{escape(snapshot["error"])}</div>'
        badge = '<span class="pill readonly">Utilgængelig</span>'
    else:
        tunnels = snapshot.get("tunnels") or []
        unhealthy = snapshot.get("unhealthy_tunnels") or []
        apps = snapshot.get("access_apps") or []
        records = snapshot.get("dns_records") or []
        zone = snapshot.get("zone") or {}
        body = (
            '<div class="notification-stats">'
            f'<div class="notification-stat"><span class="label">Tunneler</span><strong class="{"bad" if unhealthy else "ok"}">{len(tunnels)}</strong></div>'
            f'<div class="notification-stat"><span class="label">Access apps</span><strong>{len(apps)}</strong></div>'
            f'<div class="notification-stat"><span class="label">DNS records</span><strong>{len(records)}</strong></div>'
            '</div>'
            f'<div class="channel-list"><small>Zone: {escape(str(zone.get("name") or "–"))} · status: {escape(str(zone.get("status") or "–"))}'
            f'{" · cachet data" if snapshot.get("stale") else ""}</small></div>'
            '<p><a class="btn" href="/api/cloudflare">Vis Cloudflare JSON</a></p>'
        )
        badge = '<span class="pill admin">Read-only</span>'
    return (
        '<article class="card" id="cloudflare">'
        '<div class="section"><div><h2>Cloudflare Center</h2>'
        '<small>Tunneler, Access, DNS og zonestatus</small></div>'
        f'{badge}</div>{body}</article>'
    )


def init_cloudflare_center(app):
    app.config.setdefault("CLOUDFLARE_ACCOUNT_ID", os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip())
    app.config.setdefault("CLOUDFLARE_ZONE_ID", os.getenv("CLOUDFLARE_ZONE_ID", "").strip())
    app.config.setdefault("CLOUDFLARE_API_TOKEN", os.getenv("CLOUDFLARE_API_TOKEN", "").strip())
    app.config.setdefault("CLOUDFLARE_CACHE_SECONDS", int(os.getenv("CLOUDFLARE_CACHE_SECONDS", "120")))
    app.config.setdefault("CLOUDFLARE_TIMEOUT_SECONDS", int(os.getenv("CLOUDFLARE_TIMEOUT_SECONDS", "8")))
    app.register_blueprint(cloudflare_blueprint)

    @app.after_request
    def expose_cloudflare(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            payload["cloudflare"] = status()
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            response.set_data(html.replace(marker, _card(status()) + marker, 1))
            response.content_length = len(response.get_data())
        return response
