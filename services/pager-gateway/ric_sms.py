from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import g, jsonify, request


PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
RIC_RE = re.compile(r"^\d{4,10}$")
MAX_SMS_CHARS = 160


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(value: Any) -> str:
    phone = re.sub(r"[\s()-]", "", str(value or "").strip())
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.isdigit() and len(phone) == 8:
        phone = "+45" + phone
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("Telefonnummeret er ugyldigt")
    return phone


def normalize_ric(value: Any) -> str:
    ric = str(value or "").strip()
    if not RIC_RE.fullmatch(ric):
        raise ValueError("RIC/capcode skal være 4-10 cifre")
    return ric


def normalize_gateway_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("SMS Gateway URL skal være en gyldig http/https-adresse")
    if parsed.username or parsed.password:
        raise ValueError("SMS Gateway URL må ikke indeholde brugernavn/adgangskode")
    return raw


def format_alarm_sms(event: dict[str, Any]) -> str:
    station = " ".join(str(event.get("station") or "Pageralarm").split())
    message = " ".join(str(event.get("message") or "").split())
    text = f"RACHER PAGER\n{station}\n{message}".strip()
    if len(text) <= MAX_SMS_CHARS:
        return text
    return text[: MAX_SMS_CHARS - 1].rstrip() + "…"


class RicSmsStore:
    """Persistent RIC -> SMS routing and delivery audit in pager.db."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ric_sms_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ric TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    created_by INTEGER,
                    UNIQUE(ric, phone)
                );
                CREATE INDEX IF NOT EXISTS idx_ric_sms_rules_ric
                    ON ric_sms_rules(ric, active);

                CREATE TABLE IF NOT EXISTS ric_sms_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    recipient TEXT NOT NULL,
                    matched_rics TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gateway_message_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(message_id, recipient)
                );
                CREATE INDEX IF NOT EXISTS idx_ric_sms_delivery_created
                    ON ric_sms_deliveries(id DESC);

                INSERT OR IGNORE INTO settings(key, value) VALUES ('ric_sms_enabled', '0');
                INSERT OR IGNORE INTO settings(key, value) VALUES ('ric_sms_gateway_url', '');
                """
            )
            conn.commit()

    def config(self) -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('ric_sms_enabled','ric_sms_gateway_url')"
            ).fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        env_url = normalize_gateway_url(os.getenv("PAGER_SMS_GATEWAY_URL", ""))
        stored_url = normalize_gateway_url(values.get("ric_sms_gateway_url", ""))
        return {
            "enabled": values.get("ric_sms_enabled", "0") == "1",
            "gateway_url": stored_url or env_url,
            "gateway_url_source": "database" if stored_url else ("environment" if env_url else "unset"),
        }

    def update_config(self, *, enabled: Any, gateway_url: Any) -> dict[str, Any]:
        clean_url = normalize_gateway_url(gateway_url)
        clean_enabled = bool(enabled)
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('ric_sms_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if clean_enabled else "0",),
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('ric_sms_gateway_url',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (clean_url,),
            )
            conn.commit()
        return self.config()

    def list_rules(self) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT id, ric, phone, label, active, created_at, created_by "
                "FROM ric_sms_rules ORDER BY ric, label COLLATE NOCASE, phone"
            ).fetchall()
        return [
            {**dict(row), "active": bool(row["active"])}
            for row in rows
        ]

    def add_rule(self, ric: Any, phone: Any, label: Any = "", active: Any = True,
                 created_by: int | None = None) -> dict[str, Any]:
        clean_ric = normalize_ric(ric)
        clean_phone = normalize_phone(phone)
        clean_label = " ".join(str(label or "").strip().split())[:120]
        with self._lock, self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO ric_sms_rules(ric, phone, label, active, created_at, created_by)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (clean_ric, clean_phone, clean_label, 1 if bool(active) else 0, _now(), created_by),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Der findes allerede en SMS-regel for denne RIC og dette nummer") from exc
            row = conn.execute(
                "SELECT id, ric, phone, label, active, created_at, created_by FROM ric_sms_rules WHERE id=?",
                (int(cur.lastrowid),),
            ).fetchone()
            conn.commit()
        result = dict(row)
        result["active"] = bool(result["active"])
        return result

    def update_rule(self, rule_id: int, *, phone: Any | None = None,
                    label: Any | None = None, active: Any | None = None) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            existing = conn.execute("SELECT * FROM ric_sms_rules WHERE id=?", (int(rule_id),)).fetchone()
            if existing is None:
                return None
            clean_phone = normalize_phone(phone) if phone is not None else str(existing["phone"])
            clean_label = (
                " ".join(str(label or "").strip().split())[:120]
                if label is not None else str(existing["label"] or "")
            )
            clean_active = 1 if (bool(active) if active is not None else bool(existing["active"])) else 0
            try:
                conn.execute(
                    "UPDATE ric_sms_rules SET phone=?, label=?, active=? WHERE id=?",
                    (clean_phone, clean_label, clean_active, int(rule_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Der findes allerede en SMS-regel for denne RIC og dette nummer") from exc
            row = conn.execute(
                "SELECT id, ric, phone, label, active, created_at, created_by FROM ric_sms_rules WHERE id=?",
                (int(rule_id),),
            ).fetchone()
            conn.commit()
        result = dict(row)
        result["active"] = bool(result["active"])
        return result

    def delete_rule(self, rule_id: int) -> bool:
        with self._lock, self.connect() as conn:
            cur = conn.execute("DELETE FROM ric_sms_rules WHERE id=?", (int(rule_id),))
            conn.commit()
        return cur.rowcount > 0

    def rules_for_rics(self, rics: set[str]) -> list[dict[str, Any]]:
        clean = sorted({str(value).strip() for value in rics if str(value).strip()})
        if not clean:
            return []
        placeholders = ",".join("?" for _ in clean)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT id, ric, phone, label, active FROM ric_sms_rules "
                f"WHERE active=1 AND ric IN ({placeholders}) ORDER BY id",
                clean,
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_delivery(self, message_id: int, recipient: str, matched_rics: set[str]) -> bool:
        now = _now()
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO ric_sms_deliveries(
                       message_id, recipient, matched_rics, status, created_at, updated_at
                   ) VALUES (?, ?, ?, 'pending', ?, ?)""",
                (int(message_id), recipient, ",".join(sorted(matched_rics)), now, now),
            )
            conn.commit()
        return cur.rowcount > 0

    def finish_delivery(self, message_id: int, recipient: str, *, status: str,
                        gateway_message_id: Any = None, error: Any = None) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """UPDATE ric_sms_deliveries
                   SET status=?, gateway_message_id=?, error=?, updated_at=?
                   WHERE message_id=? AND recipient=?""",
                (
                    str(status)[:24],
                    str(gateway_message_id)[:80] if gateway_message_id is not None else None,
                    str(error)[:1000] if error else None,
                    _now(), int(message_id), recipient,
                ),
            )
            conn.commit()

    def list_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ric_sms_deliveries ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class RicSmsRouter:
    def __init__(self, core: Any) -> None:
        self.core = core
        self.store = RicSmsStore(core.DB_PATH)
        self._original_notify = core.maybe_notify_pushover
        core.maybe_notify_pushover = self.notify_and_sms

    @staticmethod
    def _event_rics(event: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        values = event.get("burst_rics")
        if isinstance(values, (list, tuple, set)):
            result.update(str(value).strip() for value in values if str(value).strip())
        if event.get("ric"):
            result.add(str(event["ric"]).strip())
        return result

    def _allowed_rics(self, event: dict[str, Any]) -> set[str]:
        result = self._event_rics(event)
        ric_filter = getattr(self.core, "ric_noise_filter", None)
        if ric_filter is None:
            return result
        allowed: set[str] = set()
        for ric in result:
            try:
                if not ric_filter.contains(ric):
                    allowed.add(ric)
            except Exception:
                # Fail safe for SMS: if blocklist lookup itself fails, do not create
                # a side-channel SMS that the main delivery policy may have blocked.
                continue
        return allowed

    def _post_outgoing(self, gateway_url: str, recipient: str, body: str) -> dict[str, Any]:
        endpoint = gateway_url.rstrip("/") + "/api/outgoing"
        payload = json.dumps({"recipient": recipient, "body": body}, ensure_ascii=False).encode("utf-8")
        outgoing = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(outgoing, timeout=8) as response:
                raw = response.read().decode("utf-8")
                if response.status not in {200, 201, 202}:
                    raise RuntimeError(f"SMS Gateway svarede HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SMS Gateway svarede HTTP {exc.code}: {details[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Kunne ikke kontakte SMS Gateway: {exc.reason}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("SMS Gateway returnerede ugyldigt JSON") from exc

    def _send_reserved(self, message_id: int, recipient: str, matched_rics: set[str],
                       gateway_url: str, body: str) -> None:
        try:
            result = self._post_outgoing(gateway_url, recipient, body)
            self.store.finish_delivery(
                message_id, recipient,
                status="queued",
                gateway_message_id=result.get("id"),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.finish_delivery(message_id, recipient, status="failed", error=exc)
            self.core.app.logger.warning(
                "RIC SMS failed for message %s to %s: %s", message_id, recipient, exc
            )

    def queue_for_event(self, message_id: int, event: dict[str, Any]) -> int:
        if not event.get("delivery_eligible", True):
            return 0
        if not str(event.get("source") or "").lower().startswith("pdl"):
            return 0
        config = self.store.config()
        if not config["enabled"] or not config["gateway_url"]:
            return 0
        rics = self._allowed_rics(event)
        if not rics:
            return 0
        rules = self.store.rules_for_rics(rics)
        if not rules:
            return 0

        recipients: dict[str, set[str]] = {}
        for rule in rules:
            recipients.setdefault(str(rule["phone"]), set()).add(str(rule["ric"]))

        body = format_alarm_sms(event)
        queued = 0
        for recipient, matched_rics in recipients.items():
            if not self.store.reserve_delivery(message_id, recipient, matched_rics):
                continue
            threading.Thread(
                target=self._send_reserved,
                args=(message_id, recipient, matched_rics, config["gateway_url"], body),
                name=f"ric-sms-{message_id}",
                daemon=True,
            ).start()
            queued += 1
        return queued

    def notify_and_sms(self, message_id: int, event: dict[str, Any]) -> None:
        try:
            self.queue_for_event(message_id, event)
        except Exception as exc:  # noqa: BLE001
            self.core.app.logger.warning("RIC SMS routing failed for message %s: %s", message_id, exc)
        return self._original_notify(message_id, event)

    def test_sms(self, recipient: str) -> dict[str, Any]:
        config = self.store.config()
        if not config["gateway_url"]:
            raise ValueError("SMS Gateway URL mangler")
        phone = normalize_phone(recipient)
        return self._post_outgoing(config["gateway_url"], phone, "RACHER PAGER\nTest-SMS fra Pager Gateway")


def register_ric_sms_routes(core: Any, router: RicSmsRouter, auth_required: Callable) -> None:
    app = core.app
    storage = core.storage
    store = router.store

    @app.get("/api/ric-sms/config")
    @auth_required(admin=True)
    def api_ric_sms_config_get():
        return jsonify(store.config())

    @app.put("/api/ric-sms/config")
    @auth_required(admin=True)
    def api_ric_sms_config_put():
        body = request.get_json(silent=True) or {}
        try:
            config = store.update_config(
                enabled=bool(body.get("enabled")),
                gateway_url=body.get("gateway_url", ""),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "ric-sms-config",
            f"enabled={int(config['enabled'])}; gateway={config['gateway_url'] or '-'}",
        )
        return jsonify({"ok": True, "config": config})

    @app.get("/api/ric-sms/rules")
    @auth_required(admin=True)
    def api_ric_sms_rules_get():
        return jsonify(store.list_rules())

    @app.post("/api/ric-sms/rules")
    @auth_required(admin=True)
    def api_ric_sms_rules_post():
        body = request.get_json(silent=True) or {}
        try:
            rule = store.add_rule(
                body.get("ric"), body.get("phone"), body.get("label", ""),
                bool(body.get("active", True)), int(g.user["id"]),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        storage.add_audit(
            g.user["id"], "ric-sms-rule-add",
            f"rule_id={rule['id']}; ric={rule['ric']}; phone={rule['phone']}",
        )
        return jsonify({"ok": True, "rule": rule}), 201

    @app.patch("/api/ric-sms/rules/<int:rule_id>")
    @auth_required(admin=True)
    def api_ric_sms_rules_patch(rule_id: int):
        body = request.get_json(silent=True) or {}
        try:
            rule = store.update_rule(
                rule_id,
                phone=body.get("phone") if "phone" in body else None,
                label=body.get("label") if "label" in body else None,
                active=body.get("active") if "active" in body else None,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if rule is None:
            return jsonify({"ok": False, "error": "SMS-reglen findes ikke"}), 404
        storage.add_audit(g.user["id"], "ric-sms-rule-update", f"rule_id={rule_id}")
        return jsonify({"ok": True, "rule": rule})

    @app.delete("/api/ric-sms/rules/<int:rule_id>")
    @auth_required(admin=True)
    def api_ric_sms_rules_delete(rule_id: int):
        if not store.delete_rule(rule_id):
            return jsonify({"ok": False, "error": "SMS-reglen findes ikke"}), 404
        storage.add_audit(g.user["id"], "ric-sms-rule-delete", f"rule_id={rule_id}")
        return jsonify({"ok": True})

    @app.get("/api/ric-sms/deliveries")
    @auth_required(admin=True)
    def api_ric_sms_deliveries():
        return jsonify(store.list_deliveries(limit=request.args.get("limit", 40)))

    @app.post("/api/ric-sms/test")
    @auth_required(admin=True)
    def api_ric_sms_test():
        body = request.get_json(silent=True) or {}
        try:
            result = router.test_sms(body.get("phone", ""))
        except (ValueError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        storage.add_audit(g.user["id"], "ric-sms-test", "test-SMS queued")
        return jsonify({"ok": True, "gateway": result})


def install_ric_sms(core: Any, auth_required: Callable) -> RicSmsRouter:
    router = RicSmsRouter(core)
    register_ric_sms_routes(core, router, auth_required)
    core.ric_sms_router = router
    return router
