from flask import Blueprint, current_app, jsonify, request

from services.database_service import open_database
from services.docker_service import ContainerNotFoundError, container_logs
from services.observability_service import history_snapshot, log_snapshot

observability_blueprint = Blueprint("observability", __name__)


def database():
    return open_database(
        current_app.config["DATA_ROOT"],
        current_app.config["DATABASE_PATH"],
    )


@observability_blueprint.get("/api/observability/history")
def api_observability_history():
    hours = request.args.get("hours", default=24, type=int)
    return jsonify(history_snapshot(hours, database))


@observability_blueprint.get("/api/observability/logs/<container_name>")
def api_observability_logs(container_name):
    tail = request.args.get("tail", default=200, type=int)
    query = request.args.get("q", default="", type=str)
    try:
        return jsonify(
            log_snapshot(
                container_name,
                tail=tail,
                query=query,
                logs_loader=container_logs,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ContainerNotFoundError:
        return jsonify({"error": "Containeren blev ikke fundet."}), 404
    except Exception:
        return jsonify({"error": "Logdata kunne ikke hentes."}), 503


def _card():
    return (
        '<article class="card" id="observability">'
        '<div class="section"><div><h2>Metrics & Log Explorer</h2>'
        '<small>Historiske systemmålinger og redigerede containerlogs</small></div>'
        '<span class="pill readonly">Read-only</span></div>'
        '<div class="notification-stats">'
        '<div class="notification-stat"><span class="label">Periode</span><strong>24 timer</strong></div>'
        '<div class="notification-stat"><span class="label">Målinger</span><strong id="metric-count">–</strong></div>'
        '<div class="notification-stat"><span class="label">Retention</span><strong>30 dage</strong></div>'
        '</div>'
        '<canvas id="metrics-chart" width="760" height="220" aria-label="Historiske systemmålinger"></canvas>'
        '<p><a class="btn" href="/api/observability/history?hours=24">Vis måledata</a> '
        '<a class="btn" href="/api/observability/logs/control-center?tail=100">Control Center-log</a></p>'
        '<script>'
        '(async()=>{try{const r=await fetch("/api/observability/history?hours=24");'
        'const d=await r.json();document.getElementById("metric-count").textContent=d.count;'
        'const c=document.getElementById("metrics-chart"),x=c.getContext("2d"),p=d.points||[];'
        'x.clearRect(0,0,c.width,c.height);x.strokeStyle="#4b5563";x.strokeRect(35,10,c.width-45,c.height-35);'
        'const series=[["cpu","CPU"],["ram","RAM"],["disk","Disk"],["temperature","Temp"]];'
        'series.forEach((s,si)=>{x.beginPath();x.strokeStyle=["#60a5fa","#34d399","#f59e0b","#f87171"][si];'
        'p.forEach((v,i)=>{const n=Number(v[s[0]]);if(!Number.isFinite(n))return;'
        'const px=35+(i/Math.max(p.length-1,1))*(c.width-45);const py=10+(1-Math.min(Math.max(n,0),100)/100)*(c.height-35);'
        'if(i===0)x.moveTo(px,py);else x.lineTo(px,py)});x.stroke();x.fillStyle=x.strokeStyle;x.fillText(s[1],45+si*70,25)});'
        '}catch(e){document.getElementById("metric-count").textContent="Fejl"}})();'
        '</script></article>'
    )


def init_observability(app):
    app.register_blueprint(observability_blueprint)

    @app.after_request
    def expose_observability(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            snapshot = history_snapshot(24, database)
            payload["observability"] = {
                "history_points": snapshot["count"],
                "retention_days": 30,
                "logs_redacted": True,
            }
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            response.set_data(html.replace(marker, _card() + marker, 1))
            response.content_length = len(response.get_data())
        return response
