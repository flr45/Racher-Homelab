from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from flask import g, jsonify, request


_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "ja", "on"}


def normalize_phone(value: Any) -> str:
    phone = re.sub(r"[\s().-]", "", str(value or "").strip())
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not _PHONE_RE.fullmatch(phone):
        raise ValueError("WhatsApp-nummeret skal være et gyldigt internationalt nummer, fx +4512345678.")
    return phone


class OpenWAClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("PAGER_OPENWA_URL", "http://127.0.0.1:2785").rstrip("/")
        self.api_key = os.getenv("PAGER_OPENWA_API_KEY", "").strip()
        self.session_id = os.getenv("PAGER_OPENWA_SESSION", "pager").strip() or "pager"
        self.timeout = max(2.0, min(float(os.getenv("PAGER_OPENWA_TIMEOUT", "10")), 30.0))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.session_id)

    def send_text(self, phone: str, text: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("OpenWA er ikke konfigureret. Sæt PAGER_OPENWA_API_KEY og session-id.")
        chat_id = normalize_phone(phone).lstrip("+") + "@c.us"
        session = urllib.parse.quote(self.session_id, safe="")
        url = f"{self.base_url}/api/sessions/{session}/messages/send-text"
        body = json.dumps({"chatId": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
                "User-Agent": "Racher-Pager-Gateway/WhatsApp",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
                return json.loads(payload) if payload else {"ok": True}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"OpenWA HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenWA kan ikke kontaktes: {exc.reason}") from exc


class WhatsAppDelivery:
    def __init__(self, app, storage, routing) -> None:
        self.app = app
        self.storage = storage
        self.routing = routing
        self.client = OpenWAClient()
        self._initialize()

    def _initialize(self) -> None:
        with self.storage.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_whatsapp_preferences (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    phone_e164 TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS whatsapp_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER,
                    user_id INTEGER NOT NULL,
                    phone_e164 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    openwa_message_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(message_id, user_id),
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_whatsapp_delivery_message ON whatsapp_deliveries(message_id, status);
                CREATE INDEX IF NOT EXISTS idx_whatsapp_delivery_user ON whatsapp_deliveries(user_id, id DESC);
                """
            )

    def get_preference(self, user_id: int) -> dict[str, Any]:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT user_id, enabled, phone_e164, updated_at FROM user_whatsapp_preferences WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return {"user_id": user_id, "enabled": False, "phone_e164": "", "updated_at": None}
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def set_preference(self, user_id: int, *, enabled: bool, phone: str) -> dict[str, Any]:
        normalized = normalize_phone(phone) if phone else ""
        if enabled and not normalized:
            raise ValueError("Indtast et WhatsApp-nummer før WhatsApp aktiveres.")
        with self.storage.connect() as conn:
            conn.execute(
                """INSERT INTO user_whatsapp_preferences(user_id, enabled, phone_e164, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     enabled=excluded.enabled,
                     phone_e164=excluded.phone_e164,
                     updated_at=excluded.updated_at""",
                (user_id, 1 if enabled else 0, normalized, _now()),
            )
        return self.get_preference(user_id)

    def list_user_preferences(self) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            rows = conn.execute(
                """SELECT u.id AS user_id, u.username, u.display_name, u.active,
                          COALESCE(w.enabled,0) AS enabled, COALESCE(w.phone_e164,'') AS phone_e164,
                          w.updated_at
                   FROM users u
                   LEFT JOIN user_whatsapp_preferences w ON w.user_id=u.id
                   ORDER BY u.role, u.display_name COLLATE NOCASE"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def recipients_for_event(self, station: str | None) -> list[dict[str, Any]]:
        key = self.routing.station_key(station)
        with self.storage.connect() as conn:
            if key:
                rows = conn.execute(
                    """SELECT DISTINCT u.id AS user_id, u.display_name, w.phone_e164
                       FROM users u
                       JOIN user_whatsapp_preferences w ON w.user_id=u.id AND w.enabled=1
                       LEFT JOIN user_routing_preferences p ON p.user_id=u.id
                       LEFT JOIN user_station_subscriptions us ON us.user_id=u.id
                       WHERE u.active=1 AND w.phone_e164<>''
                         AND (COALESCE(p.receive_all,0)=1 OR us.station_key=?)
                       ORDER BY u.id""",
                    (key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT DISTINCT u.id AS user_id, u.display_name, w.phone_e164
                       FROM users u
                       JOIN user_whatsapp_preferences w ON w.user_id=u.id AND w.enabled=1
                       LEFT JOIN user_routing_preferences p ON p.user_id=u.id
                       WHERE u.active=1 AND w.phone_e164<>''
                         AND (u.role='admin' OR COALESCE(p.receive_all,0)=1)
                       ORDER BY u.id"""
                ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def format_alarm(event: dict[str, Any]) -> str:
        station = str(event.get("station") or "Pageralarm")
        message = str(event.get("message") or "").strip()
        ric = str(event.get("ric") or "").strip()
        received = str(event.get("received_at") or "").strip()
        lines = [f"🚨 ALARM – {station}", "", message]
        meta = []
        if ric:
            meta.append(f"RIC {ric}")
        if received:
            try:
                dt = datetime.fromisoformat(received.replace("Z", "+00:00")).astimezone()
                meta.append(dt.strftime("%d-%m-%Y %H:%M:%S"))
            except ValueError:
                meta.append(received)
        if meta:
            lines.extend(["", " · ".join(meta)])
        return "\n".join(lines)[:4096]

    def _reserve_delivery(self, message_id: int, user_id: int, phone: str) -> bool:
        now = _now()
        with self.storage.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO whatsapp_deliveries(
                       message_id, user_id, phone_e164, status, created_at, updated_at
                   ) VALUES (?, ?, ?, 'queued', ?, ?)""",
                (message_id, user_id, phone, now, now),
            )
            return cur.rowcount == 1

    def _finish_delivery(self, message_id: int, user_id: int, *, status: str,
                         openwa_message_id: str = "", error: str = "") -> None:
        with self.storage.connect() as conn:
            conn.execute(
                """UPDATE whatsapp_deliveries
                   SET status=?, openwa_message_id=?, error=?, updated_at=?
                   WHERE message_id=? AND user_id=?""",
                (status, openwa_message_id[:200], error[:1000], _now(), message_id, user_id),
            )

    def dispatch(self, message_id: int, event: dict[str, Any]) -> None:
        if not event.get("delivery_eligible", True):
            return
        if not _as_bool(os.getenv("PAGER_WHATSAPP_ENABLED", "0")):
            return
        if not self.client.configured:
            self.app.logger.warning("WhatsApp delivery enabled but OpenWA is not configured")
            return
        text = self.format_alarm(event)
        for recipient in self.recipients_for_event(event.get("station")):
            user_id = int(recipient["user_id"])
            phone = str(recipient["phone_e164"])
            if not self._reserve_delivery(message_id, user_id, phone):
                continue
            try:
                result = self.client.send_text(phone, text)
                remote_id = str(result.get("messageId") or result.get("id") or "")
                self._finish_delivery(message_id, user_id, status="sent", openwa_message_id=remote_id)
            except Exception as exc:
                self._finish_delivery(message_id, user_id, status="failed", error=str(exc))
                self.app.logger.warning("WhatsApp failed for message=%s user=%s: %s", message_id, user_id, exc)

    def dispatch_async(self, message_id: int, event: dict[str, Any]) -> None:
        threading.Thread(
            target=self.dispatch,
            args=(message_id, dict(event)),
            name=f"whatsapp-{message_id}",
            daemon=True,
        ).start()

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self.storage.connect() as conn:
            rows = conn.execute(
                """SELECT d.*, u.display_name, m.station, m.message
                   FROM whatsapp_deliveries d
                   JOIN users u ON u.id=d.user_id
                   LEFT JOIN messages m ON m.id=d.message_id
                   ORDER BY d.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def install_whatsapp(app, storage, routing, auth_required) -> WhatsAppDelivery:
    delivery = WhatsAppDelivery(app, storage, routing)

    # Patch the shared app_core ingest symbol. on_pdl_line and the simulator route
    # resolve that symbol in app_core's module globals, so both real and simulated
    # alarms use exactly the same WhatsApp dispatch point after filtering/routing.
    import app_core

    original_ingest = app_core.ingest_event

    def ingest_with_whatsapp(event):
        message_id = original_ingest(event)
        try:
            with storage.connect() as conn:
                row = conn.execute("SELECT * FROM messages WHERE id=?", (int(message_id),)).fetchone()
            event_row = dict(row) if row else None
            if event_row and event_row.get("delivery_eligible"):
                delivery.dispatch_async(message_id, event_row)
        except Exception:
            app.logger.exception("Unable to queue WhatsApp delivery for message %s", message_id)
        return message_id

    app_core.ingest_event = ingest_with_whatsapp

    @app.get("/api/whatsapp/me")
    @auth_required()
    def whatsapp_me_get():
        return jsonify({
            **delivery.get_preference(int(g.user["id"])),
            "gateway_enabled": _as_bool(os.getenv("PAGER_WHATSAPP_ENABLED", "0")),
            "gateway_configured": delivery.client.configured,
        })

    @app.put("/api/whatsapp/me")
    @auth_required()
    def whatsapp_me_put():
        payload = request.get_json(silent=True) or {}
        try:
            preference = delivery.set_preference(
                int(g.user["id"]),
                enabled=_as_bool(payload.get("enabled")),
                phone=str(payload.get("phone_e164") or ""),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(int(g.user["id"]), "whatsapp-preference", f"enabled={int(preference['enabled'])}")
        return jsonify({"ok": True, **preference})

    @app.get("/api/whatsapp/users")
    @auth_required(admin=True)
    def whatsapp_users():
        return jsonify(delivery.list_user_preferences())

    @app.put("/api/whatsapp/users/<int:user_id>")
    @auth_required(admin=True)
    def whatsapp_user_admin_update(user_id: int):
        if not storage.get_user(user_id):
            return jsonify({"ok": False, "error": "Brugeren findes ikke."}), 404
        payload = request.get_json(silent=True) or {}
        try:
            preference = delivery.set_preference(
                user_id,
                enabled=_as_bool(payload.get("enabled")),
                phone=str(payload.get("phone_e164") or ""),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(int(g.user["id"]), "whatsapp-user-update", f"user_id={user_id}; enabled={int(preference['enabled'])}")
        return jsonify({"ok": True, **preference})

    @app.post("/api/whatsapp/test")
    @auth_required()
    def whatsapp_test():
        preference = delivery.get_preference(int(g.user["id"]))
        if not preference.get("phone_e164"):
            return jsonify({"ok": False, "error": "Gem først dit WhatsApp-nummer."}), 400
        try:
            result = delivery.client.send_text(
                str(preference["phone_e164"]),
                "✅ Test fra Racher Pager Gateway\n\nWhatsApp-levering virker.",
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "result": result})

    @app.get("/api/whatsapp/deliveries")
    @auth_required(admin=True)
    def whatsapp_deliveries():
        return jsonify(delivery.list_recent())

    @app.get("/api/whatsapp/status")
    @auth_required(admin=True)
    def whatsapp_status():
        return jsonify({
            "enabled": _as_bool(os.getenv("PAGER_WHATSAPP_ENABLED", "0")),
            "configured": delivery.client.configured,
            "url": delivery.client.base_url,
            "session": delivery.client.session_id,
            "api_key_set": bool(delivery.client.api_key),
        })

    @app.after_request
    def inject_whatsapp_ui(response):
        content_type = str(response.content_type or "")
        if request.path != "/" or response.status_code != 200 or not content_type.startswith("text/html"):
            return response
        html = response.get_data(as_text=True)
        marker = "</body>"
        script = '<script src="/static/whatsapp.js" defer></script>'
        if marker in html and script not in html:
            response.set_data(html.replace(marker, script + marker))
            response.content_length = len(response.get_data())
        return response

    return delivery
