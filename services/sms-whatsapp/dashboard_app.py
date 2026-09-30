"""Unified administration UI; keep the existing alarm-processing extensions."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import timedelta

from flask import flash, jsonify, redirect, render_template, request, url_for

import delivery_retry_app as deliveries
import admin_station_app as users
import history_app as history
import station_filter_app as stations
import station_names_app as names

app = deliveries.app
base = deliveries.base
db = base.db
events = stations.events
_gateway_cache = (0.0, {})
_gateway_lock = threading.Lock()


def gateway_request(path="/health", method="GET"):
    root = os.getenv("SMS_WHATSAPP_SMS_GATEWAY_URL", "http://sms-gateway:8080").rstrip("/")
    token = os.getenv("SMS_GATEWAY_API_TOKEN", "")
    req = urllib.request.Request(root + path, data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=4 if method == "GET" else 45) as response:
        return json.load(response)


def gateway_status():
    global _gateway_cache
    with _gateway_lock:
        if time.monotonic() - _gateway_cache[0] < 10:
            return dict(_gateway_cache[1])
        try:
            data = gateway_request()
        except Exception:
            data = {"modem": {"state": "offline", "transport": "unknown", "last_error": "SMS Gateway kunne ikke kontaktes"}, "gateway": {"database": "unknown"}}
        _gateway_cache = (time.monotonic(), data)
        return dict(data)


STATUS_LABELS = {"ready": "Forbundet", "online": "Online", "offline": "Offline", "missing": "Session mangler",
                 "unknown": "Ukendt", "initializing": "Starter", "connecting": "Forbinder", "degraded": "Kræver opmærksomhed",
                 "stale": "Status forældet", "recovering": "Genopretter", "pending": "I kø", "retrying": "Nyt forsøg afventer",
                 "failed": "Fejlet", "sent": "Sendt", "cancelled": "Annulleret", "qr": "Scan QR-kode"}
app.jinja_env.globals.update(status_label=lambda value: STATUS_LABELS.get(str(value).lower(), str(value)), station_name=names.station_name)


def snapshot():
    gateway = gateway_status()
    queue = deliveries.queue_snapshot()
    wa = base.openwa_status()
    modem = gateway.get("modem") or {}
    good = modem.get("state") == "online" and wa.get("state") == "ready" and not queue["failed"]
    return {"gateway": gateway, "modem": modem, "queue": queue, "wa": wa,
            "good": good, "quality": deliveries.quality_snapshot(), "stats": events.stats_snapshot()}


@base.login_required
def dashboard():
    data = snapshot()
    recent = events.AlarmEvent.query.order_by(events.AlarmEvent.started_at.desc()).limit(8).all()
    messages = base.InboundMessage.query.order_by(base.InboundMessage.id.desc()).limit(20).all()
    logs = base.WhatsAppDelivery.query.order_by(base.WhatsAppDelivery.id.desc()).limit(25).all()
    cutoff = base.utcnow() - timedelta(days=1)
    sent24 = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.status == "sent", base.WhatsAppDelivery.attempted_at >= cutoff).count()
    return render_template("dashboard.html", title="Overblik", data=data, recent=recent, messages=messages, logs=logs,
                           active_recipients=base.Recipient.query.filter_by(active=True).count(), sent24=sent24)


app.view_functions["dashboard"] = dashboard


@app.get("/indstillinger")
@base.login_required
def settings_page():
    return render_template("settings.html", title="Indstillinger", delay=stations.prealert_delay_seconds(),
                           senders=base.AllowedSender.query.order_by(base.AllowedSender.name).all())


@app.get("/forbindelser")
@base.login_required
def connections_page():
    return render_template("connections.html", title="Forbindelser", data=snapshot())


@app.post("/forbindelser/cudy-test")
@base.login_required
def cudy_connection_test():
    base.check_csrf()
    try:
        result = gateway_request("/api/cudy/probe", "POST")
        flash("Cudy svarede på forbindelsestesten. SIM: " + str(result.get("sim", "Ukendt")).strip()[:100])
    except Exception:
        flash("Cudy-testen lykkedes ikke. Kontrollér routeradresse, adgangskode og firmware i SMS Gateway.", "error")
    return redirect(url_for("connections_page"))


@app.get("/api/dashboard/status")
@base.login_required
def dashboard_status():
    data = snapshot()
    return jsonify(modem=data["modem"], openwa=data["wa"], queue=data["queue"], good=data["good"])


@app.after_request
def unified_ui(response):
    if response.status_code == 200 and "text/html" in response.content_type:
        html = response.get_data(as_text=True)
        css = '<link rel="stylesheet" href="' + url_for("static", filename="pager.css") + '">'
        html = html.replace("</head>", css + "</head>", 1)
        if base.logged_in():
            if 'id="main-content"' not in html:
                html = html.replace('<div class="wrap">', '<div class="wrap" id="main-content" tabindex="-1">', 1)
            nav = render_template("navigation.html")
            html = re.sub(r"<body([^>]*)>", lambda m: m.group(0) + nav, html, count=1)
            html = html.replace("</body>", '<script src="' + url_for("static", filename="pager.js") + '" defer></script></body>', 1)
        response.set_data(html)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Serialize deletion with delivery attempts; FK cascades remove retry states.
app.view_functions["delete_alarm_event"] = deliveries.serialized(app.view_functions["delete_alarm_event"])
