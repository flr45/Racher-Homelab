"""Unified administration UI; keep the existing alarm-processing extensions."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import timedelta

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for

import delivery_retry_app as deliveries
import admin_station_app as users
import history_app as history
import station_filter_app as stations
import station_names_app as names
import diagnostics

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
    internet = diagnostics.internet_status()
    modem = gateway.get("modem") or {}
    good = internet.get("state") == "online" and modem.get("state") == "online" and wa.get("state") == "ready" and not queue["failed"]
    ops = operations.operation_snapshot()
    good = good and ops["mode"]["name"] == "normal" and not ops["held"] and not ops["failure_warning"]
    return {"ops": ops, "gateway": gateway, "modem": modem, "queue": queue, "wa": wa,
            "internet": internet, "good": good, "quality": deliveries.quality_snapshot(), "stats": events.stats_snapshot()}


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
                           accept_all=stations.accept_all_sms_senders(),
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
    return jsonify(ops=data["ops"], internet=data["internet"], modem=data["modem"], openwa=data["wa"], queue=data["queue"], good=data["good"])


@app.get("/diagnostik")
@base.login_required
def diagnostic_page():
    messages = base.InboundMessage.query.order_by(base.InboundMessage.id.desc()).limit(50).all()
    issues = base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.status.in_(("failed", "retrying", "cancelled", "uncertain"))).order_by(base.WhatsAppDelivery.id.desc()).limit(50).all()
    return render_template("diagnostics.html", title="Fejloversigt", messages=[(row, diagnostics.explain_inbound(row)) for row in messages], issues=issues)


@app.get("/enkelt-test")
@base.login_required
def single_test_page():
    token = session.setdefault("single_test_token", diagnostics.secrets.token_urlsafe(24))
    tests = diagnostics.SingleWhatsAppTest.query.order_by(diagnostics.SingleWhatsAppTest.id.desc()).limit(20).all()
    return render_template("single_test.html", title="Test ét nummer", token=token,
        recipients=base.Recipient.query.filter_by(active=True).order_by(base.Recipient.name).all(),
        tests=[diagnostics.test_details(test) for test in tests])


@app.post("/enkelt-test")
@base.login_required
def send_single_test():
    base.check_csrf()
    if operations.sending_paused():
        abort(409, "Vælg normal drift før en afsendelsestest")
    token = request.form.get("test_token", "")
    old = diagnostics.SingleWhatsAppTest.query.filter_by(token=token).first() if token else None
    if old:
        return redirect(url_for("single_test_page"))
    if not token or token != session.get("single_test_token"):
        abort(400, "Testformularen er udløbet; genindlæs siden")
    try:
        recipient_id = int(request.form.get("recipient_id", ""))
    except ValueError:
        abort(400, "Vælg én modtager")
    recipient = db.get_or_404(base.Recipient, recipient_id)
    if not recipient.active:
        abort(400, "Modtageren er pauset")
    test = diagnostics.enqueue_single_test(recipient, token)
    session.pop("single_test_token", None)
    flash(f"Test #{test.id} er sat i kø til {recipient.name} ({recipient.phone}).")
    return redirect(url_for("single_test_page"))


@app.get("/api/enkelt-test/status")
@base.login_required
def single_test_status():
    tests = diagnostics.SingleWhatsAppTest.query.order_by(diagnostics.SingleWhatsAppTest.id.desc()).limit(20).all()
    return jsonify(tests=[diagnostics.test_details(test) for test in tests])


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
            if operator_accounts.identity()['role'] == 'viewer':
                html = html.replace('<body', '<body data-readonly="true"', 1)
                notice = '<div class="flash readonly-notice">Læseadgang · du kan se status og historik. Ændringer og afsendelse kræver en administrator.</div>'
                html = re.sub(r'<(?:main|div)[^>]*id="main-content"[^>]*>', lambda m: m.group(0) + notice, html, count=1)
            html = html.replace("</body>", '<script src="' + url_for("static", filename="pager.js") + '" defer></script></body>', 1)
        response.set_data(html)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Serialize deletion with delivery attempts; FK cascades remove retry states.
for _delete_endpoint in ("delete_alarm_event", "delete_alarm_events"):
    app.view_functions[_delete_endpoint] = deliveries.serialized(app.view_functions[_delete_endpoint])


# Import after the existing routes so extension callbacks cannot form a cycle.
import operations
app.config["PAGER_OPERATION_ENDPOINTS"] = ["message_detail"]
STATUS_LABELS.update(sending="Afsender", held="Afventer godkendelse", uncertain="Ukendt udfald", pilot="Prøvetilstand")
# Configuration writes must not change recipients halfway through a send.
for _endpoint in operations.AUDIT_ENDPOINTS:
    if _endpoint in app.view_functions:
        app.view_functions[_endpoint] = operations.audited(app.view_functions[_endpoint])
_original_group_test = app.view_functions["test_message"]
@base.login_required
@deliveries.serialized
def guarded_group_test():
    if operations.sending_paused():
        abort(409, "Vælg normal drift før en afsendelsestest")
    return _original_group_test()
app.view_functions["test_message"] = guarded_group_test

import observability
import operator_accounts
import offsite_backup
import resilience

# No pending job may run before operational controls and crash recovery.
with app.app_context():
    operations.recover_interrupted_alarms()
deliveries.start_retry_worker()
resilience.start_monitor()
offsite_backup.start_worker()
