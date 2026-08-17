from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


_KEY_RE = re.compile(r"^[A-Za-z0-9]{20,80}$")
_MAX_LABEL_LENGTH = 80
_MAX_DESTINATIONS = 25


def mask_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        return "—"
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:4]}{'•' * max(6, len(key) - 10)}{key[-6:]}"


class PushoverDestinationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pushover_destinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL,
                    user_key TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    created_by INTEGER,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pushover_destinations_active ON pushover_destinations(active, id)"
            )

    @staticmethod
    def _validate_label(value: Any) -> str:
        label = re.sub(r"\s+", " ", str(value or "")).strip()
        if not label:
            label = "Pushover-modtager"
        if len(label) > _MAX_LABEL_LENGTH:
            raise ValueError(f"Navnet må højst være {_MAX_LABEL_LENGTH} tegn.")
        return label

    @staticmethod
    def _validate_key(value: Any) -> str:
        key = str(value or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError("Pushover user/group key ser ikke gyldig ud.")
        return key

    def count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM pushover_destinations").fetchone()
        return int(row["count"])

    def add(self, label: Any, user_key: Any, created_by: int | None = None) -> dict[str, Any]:
        name = self._validate_label(label)
        key = self._validate_key(user_key)
        if self.count() >= _MAX_DESTINATIONS:
            raise ValueError(f"Der kan højst være {_MAX_DESTINATIONS} Pushover-modtagere.")
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO pushover_destinations(label, user_key, active, created_at, created_by) VALUES (?, ?, 1, ?, ?)",
                (name, key, now, created_by),
            )
            row = conn.execute("SELECT * FROM pushover_destinations WHERE id=?", (int(cur.lastrowid),)).fetchone()
        return self.public_row(dict(row))

    def list_all(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pushover_destinations ORDER BY active DESC, label COLLATE NOCASE, id"
            ).fetchall()
        return [self.public_row(dict(row)) for row in rows]

    def list_active_secret(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, label, user_key FROM pushover_destinations WHERE active=1 ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_active(self, destination_id: int, active: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pushover_destinations SET active=? WHERE id=?",
                (1 if active else 0, int(destination_id)),
            )
            row = conn.execute("SELECT * FROM pushover_destinations WHERE id=?", (int(destination_id),)).fetchone()
        return self.public_row(dict(row)) if row else None

    def delete(self, destination_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM pushover_destinations WHERE id=?", (int(destination_id),))
        return cur.rowcount == 1

    @staticmethod
    def public_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "label": str(row.get("label") or "Pushover-modtager"),
            "key_masked": mask_key(str(row.get("user_key") or "")),
            "active": bool(row.get("active")),
            "created_at": str(row.get("created_at") or ""),
        }


def install_pushover_destinations(core: Any) -> PushoverDestinationStore:
    store = PushoverDestinationStore(core.DB_PATH)

    # One-time migration from the original single global user/group key. Clear the
    # legacy secret only after it has been copied so deleting all managed
    # destinations later does not resurrect an old address on the next restart.
    legacy_key = str(core.setting("pushover_user_key", "") or "").strip()
    if legacy_key and store.count() == 0:
        try:
            store.add("Primær modtager", legacy_key, None)
            core.storage.update_settings({"pushover_user_key": ""})
        except (ValueError, sqlite3.IntegrityError) as exc:
            core.app.logger.warning("Could not migrate legacy Pushover destination: %s", exc)

    @core.app.get("/api/pushover/destinations")
    @core.auth_required(admin=True)
    def api_pushover_destinations_get():
        return core.jsonify({"destinations": store.list_all()})

    @core.app.post("/api/pushover/destinations")
    @core.auth_required(admin=True)
    def api_pushover_destinations_create():
        payload = core.request.get_json(silent=True) or {}
        try:
            row = store.add(payload.get("label"), payload.get("user_key"), int(core.g.user["id"]))
        except ValueError as exc:
            return core.jsonify({"ok": False, "error": str(exc)}), 400
        except sqlite3.IntegrityError:
            return core.jsonify({"ok": False, "error": "Denne Pushover-adresse er allerede tilføjet."}), 409
        core.storage.add_audit(core.g.user["id"], "pushover-destination-create", f"destination_id={row['id']}; label={row['label']}")
        return core.jsonify({"ok": True, "destination": row})

    @core.app.patch("/api/pushover/destinations/<int:destination_id>")
    @core.auth_required(admin=True)
    def api_pushover_destination_update(destination_id: int):
        payload = core.request.get_json(silent=True) or {}
        if "active" not in payload:
            return core.jsonify({"ok": False, "error": "Kun aktiv/deaktivér understøttes her."}), 400
        row = store.set_active(destination_id, core.as_bool(payload.get("active")))
        if not row:
            return core.jsonify({"ok": False, "error": "Pushover-modtageren findes ikke."}), 404
        core.storage.add_audit(core.g.user["id"], "pushover-destination-update", f"destination_id={destination_id}; active={int(row['active'])}")
        return core.jsonify({"ok": True, "destination": row})

    @core.app.delete("/api/pushover/destinations/<int:destination_id>")
    @core.auth_required(admin=True)
    def api_pushover_destination_delete(destination_id: int):
        if not store.delete(destination_id):
            return core.jsonify({"ok": False, "error": "Pushover-modtageren findes ikke."}), 404
        core.storage.add_audit(core.g.user["id"], "pushover-destination-delete", f"destination_id={destination_id}")
        return core.jsonify({"ok": True})

    def managed_notify(message_id: int, event: dict[str, Any]) -> None:
        if not event.get("delivery_eligible", True):
            return
        settings = core.storage.get_settings()
        if settings.get("pushover_enabled") != "1":
            return
        token = str(settings.get("pushover_app_token") or "").strip()
        if not token:
            return
        destinations = store.list_active_secret()
        if not destinations:
            return

        sent = 0
        for destination in destinations:
            try:
                core.pushover.send(
                    token,
                    destination["user_key"],
                    event.get("station") or settings.get("gateway_name", "Pager"),
                    core.public_message(event.get("message", "")),
                )
                sent += 1
            except Exception as exc:
                core.app.logger.warning(
                    "Pushover failed for destination %s (%s): %s",
                    destination["id"], destination["label"], exc,
                )
        if sent:
            core.storage.mark_notification_sent(message_id)

    core.maybe_notify_pushover = managed_notify

    # Keep the existing test button/endpoint, but make it test every active managed
    # destination instead of the obsolete single hidden key.
    def managed_test():
        settings = core.storage.get_settings()
        token = str(settings.get("pushover_app_token") or "").strip()
        if not token:
            return core.jsonify({"ok": False, "error": "Pushover App token mangler."}), 400
        destinations = store.list_active_secret()
        if not destinations:
            return core.jsonify({"ok": False, "error": "Ingen aktive Pushover-modtagere er tilføjet."}), 400
        sent = 0
        errors: list[str] = []
        for destination in destinations:
            try:
                core.pushover.send(
                    token,
                    destination["user_key"],
                    "Racher Pager Gateway",
                    "Testbesked fra pager-gatewayen.",
                )
                sent += 1
            except Exception as exc:
                errors.append(f"{destination['label']}: {exc}")
        if sent == 0:
            return core.jsonify({"ok": False, "error": "; ".join(errors) or "Pushover-test fejlede."}), 400
        return core.jsonify({"ok": True, "sent": sent, "failed": len(errors)})

    core.app.view_functions["api_pushover_test"] = core.auth_required(admin=True)(managed_test)
    return store
