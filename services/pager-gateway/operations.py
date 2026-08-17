from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import g, jsonify, request
from pywebpush import WebPushException


class OperationsStore:
    """Operational telemetry kept separate from immutable pager raw history.

    The core messages table remains the source of truth for decoded traffic. This
    helper adds only ingestion timestamps and per-channel delivery telemetry, so
    operational UI can answer "is this alarm current?" and "was it delivered?"
    without changing or deleting the original decoder data.
    """

    def __init__(self, db_path: str, current_alarm_minutes: int = 120) -> None:
        self.db_path = str(Path(db_path))
        self.current_alarm_minutes = max(15, min(int(current_alarm_minutes), 1440))
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        with self._lock, self.connect() as conn:
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "ingested_at" not in columns:
                # Deliberately leave pre-upgrade rows blank. They belong in
                # Historik, not in the new "current alarms" window.
                conn.execute("ALTER TABLE messages ADD COLUMN ingested_at TEXT NOT NULL DEFAULT ''")
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS trg_messages_ingested_at
                AFTER INSERT ON messages
                FOR EACH ROW WHEN NEW.ingested_at=''
                BEGIN
                    UPDATE messages
                    SET ingested_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE id=NEW.id;
                END;

                CREATE TABLE IF NOT EXISTS message_delivery (
                    message_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, channel),
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_message_delivery_status
                    ON message_delivery(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_ingested_at
                    ON messages(ingested_at DESC);
                """
            )
            conn.commit()

    def current_message_ids(self) -> set[int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=self.current_alarm_minutes)).isoformat()
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT id FROM messages
                   WHERE delivery_eligible=1 AND ingested_at!='' AND ingested_at>=?""",
                (cutoff,),
            ).fetchall()
        return {int(row["id"]) for row in rows}

    def record_delivery(
        self,
        message_id: int,
        channel: str,
        status: str,
        *,
        target_count: int = 0,
        sent_count: int = 0,
        failed_count: int = 0,
        latency_ms: int | None = None,
        last_error: str = "",
    ) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO message_delivery(
                       message_id, channel, status, target_count, sent_count,
                       failed_count, latency_ms, last_error, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(message_id, channel) DO UPDATE SET
                       status=excluded.status,
                       target_count=excluded.target_count,
                       sent_count=excluded.sent_count,
                       failed_count=excluded.failed_count,
                       latency_ms=excluded.latency_ms,
                       last_error=excluded.last_error,
                       updated_at=excluded.updated_at""",
                (
                    int(message_id), str(channel)[:30], str(status)[:30],
                    max(0, int(target_count)), max(0, int(sent_count)),
                    max(0, int(failed_count)),
                    None if latency_ms is None else max(0, int(latency_ms)),
                    str(last_error or "")[:500], self._now(),
                ),
            )
            conn.commit()

    def message_latency_ms(self, message_id: int) -> int | None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT ingested_at FROM messages WHERE id=?", (int(message_id),)).fetchone()
        if not row or not row["ingested_at"]:
            return None
        try:
            moment = datetime.fromisoformat(str(row["ingested_at"]).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds() * 1000))
        except ValueError:
            return None

    def attach_delivery(self, rows: list[dict[str, Any]], *, include_errors: bool = False) -> list[dict[str, Any]]:
        ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        if not ids:
            return rows
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self.connect() as conn:
            deliveries = conn.execute(
                f"""SELECT message_id, channel, status, target_count, sent_count,
                           failed_count, latency_ms, last_error, updated_at
                    FROM message_delivery WHERE message_id IN ({placeholders})""",
                ids,
            ).fetchall()
        by_message: dict[int, dict[str, Any]] = {}
        for item in deliveries:
            payload = dict(item)
            message_id = int(payload.pop("message_id"))
            channel = str(payload.pop("channel"))
            if not include_errors:
                payload.pop("last_error", None)
            by_message.setdefault(message_id, {})[channel] = payload
        for row in rows:
            row["delivery"] = by_message.get(int(row["id"]), {}) if row.get("id") is not None else {}
        return rows

    def quality(self, hours: int) -> dict[str, Any]:
        hours = max(1, min(int(hours), 168))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._lock, self.connect() as conn:
            row = conn.execute(
                """SELECT
                       COUNT(*) AS raw_count,
                       SUM(CASE WHEN delivery_eligible=1 THEN 1 ELSE 0 END) AS accepted_count,
                       SUM(CASE WHEN delivery_eligible=0 THEN 1 ELSE 0 END) AS suppressed_count,
                       SUM(CASE WHEN relevance_class='noise' THEN 1 ELSE 0 END) AS noise_count,
                       SUM(CASE WHEN duplicate_of IS NOT NULL OR suppressed_reason LIKE '%duplicate%' THEN 1 ELSE 0 END) AS duplicate_count,
                       SUM(CASE WHEN suppressed_reason='decoder-fragment' THEN 1 ELSE 0 END) AS fragment_count,
                       SUM(LENGTH(message)-LENGTH(REPLACE(message, '?', ''))) AS question_marks,
                       MAX(ingested_at) AS last_raw_at,
                       MAX(CASE WHEN delivery_eligible=1 THEN ingested_at ELSE '' END) AS last_alarm_at
                   FROM messages
                   WHERE ingested_at!='' AND ingested_at>=?""",
                (cutoff,),
            ).fetchone()
            delivery = conn.execute(
                """SELECT
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END) AS partial
                   FROM message_delivery WHERE updated_at>=?""",
                (cutoff,),
            ).fetchone()
        raw_count = int(row["raw_count"] or 0)
        accepted = int(row["accepted_count"] or 0)
        return {
            "hours": hours,
            "raw_count": raw_count,
            "accepted_count": accepted,
            "suppressed_count": int(row["suppressed_count"] or 0),
            "noise_count": int(row["noise_count"] or 0),
            "duplicate_count": int(row["duplicate_count"] or 0),
            "fragment_count": int(row["fragment_count"] or 0),
            "question_marks": int(row["question_marks"] or 0),
            "acceptance_percent": round((accepted / raw_count * 100.0), 1) if raw_count else 0.0,
            "last_raw_at": str(row["last_raw_at"] or ""),
            "last_alarm_at": str(row["last_alarm_at"] or ""),
            "delivery_failed": int(delivery["failed"] or 0),
            "delivery_partial": int(delivery["partial"] or 0),
        }


def install_operations(core) -> OperationsStore:
    """Attach operations telemetry to an already initialized app_core module."""
    current_minutes = 120
    try:
        import os
        current_minutes = int(os.getenv("PAGER_CURRENT_ALARM_MINUTES", "120"))
    except ValueError:
        current_minutes = 120
    ops = OperationsStore(core.DB_PATH, current_minutes)

    original_pushover = core.maybe_notify_pushover

    def tracked_pushover(message_id: int, event: dict[str, Any]) -> None:
        if not event.get("delivery_eligible", True):
            return
        settings = core.storage.get_settings()
        if settings.get("pushover_enabled") != "1":
            ops.record_delivery(message_id, "pushover", "disabled")
            return
        try:
            original_pushover(message_id, event)
        except Exception as exc:
            ops.record_delivery(
                message_id, "pushover", "failed", target_count=1, failed_count=1,
                latency_ms=ops.message_latency_ms(message_id), last_error=str(exc),
            )
            raise
        ops.record_delivery(
            message_id, "pushover", "sent", target_count=1, sent_count=1,
            latency_ms=ops.message_latency_ms(message_id),
        )

    def tracked_web_push(message_id: int, event: dict[str, Any]) -> None:
        if not event.get("delivery_eligible", True):
            return
        payload = {
            "title": event.get("station") or "Pageralarm",
            "body": core.public_message(event.get("message", "")),
            "message_id": message_id,
            "url": "/",
        }
        subscriptions = core.routing.list_push_subscriptions_for_event(
            event.get("station"), bool(event.get("delivery_eligible", True))
        )
        if not subscriptions:
            ops.record_delivery(message_id, "web_push", "no-target")
            return
        sent = 0
        failed = 0
        errors: list[str] = []
        for subscription in subscriptions:
            try:
                core.web_push.send(subscription, payload)
                sent += 1
            except WebPushException as exc:
                failed += 1
                if core.web_push.is_gone(exc):
                    core.storage.delete_push_subscription(subscription["endpoint"])
                else:
                    errors.append(str(exc))
                    core.app.logger.warning("Web Push failed for subscription %s: %s", subscription["id"], exc)
            except Exception as exc:
                failed += 1
                errors.append(str(exc))
                core.app.logger.warning("Web Push failed for subscription %s: %s", subscription["id"], exc)
        status = "sent" if sent and not failed else "partial" if sent else "failed"
        ops.record_delivery(
            message_id, "web_push", status,
            target_count=len(subscriptions), sent_count=sent, failed_count=failed,
            latency_ms=ops.message_latency_ms(message_id), last_error=" | ".join(errors[:3]),
        )

    core.maybe_notify_pushover = tracked_pushover
    core.send_web_push_for_event = tracked_web_push

    def operations_messages():
        try:
            limit = max(1, min(int(request.args.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        scope = str(request.args.get("scope") or "feed").strip().lower()
        if scope not in {"feed", "history"}:
            return jsonify({"ok": False, "error": "scope skal være feed eller history"}), 400
        if g.user["role"] == "admin":
            rows = core.storage.list_messages(limit, delivery_eligible_only=(scope == "feed"))
        else:
            rows = core.routing.list_messages_for_user(g.user["id"], limit)
        if scope == "feed":
            current_ids = ops.current_message_ids()
            rows = [row for row in rows if int(row.get("id") or 0) in current_ids]
        return jsonify(ops.attach_delivery(rows, include_errors=g.user["role"] == "admin"))

    core.app.view_functions["api_messages"] = core.auth_required()(operations_messages)

    original_status = core.app.view_functions["api_status"]

    def operations_status(*args, **kwargs):
        response = original_status(*args, **kwargs)
        if isinstance(response, tuple) or getattr(response, "status_code", 200) >= 400:
            return response
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict):
            return response
        payload["alarm_window_minutes"] = ops.current_alarm_minutes
        payload["quality"] = {
            "hour": ops.quality(1),
            "day": ops.quality(24),
        }
        return jsonify(payload)

    core.app.view_functions["api_status"] = operations_status

    def system_delivery_test():
        checks: dict[str, dict[str, Any]] = {}
        overall = True
        try:
            with core.storage.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            checks["database"] = {"status": "ok", "detail": "SQLite svarer"}
        except Exception as exc:
            checks["database"] = {"status": "failed", "detail": str(exc)[:200]}
            overall = False

        source_state = str(core.source.status.get("state") or "unknown")
        source_ok = core.setting("source_mode", "mock") != "pdl-file" or source_state in {"waiting", "running"}
        checks["source"] = {"status": "ok" if source_ok else "failed", "detail": source_state}
        overall = overall and source_ok

        settings = core.storage.get_settings()
        if settings.get("pushover_enabled") == "1":
            try:
                core.pushover.send(
                    settings.get("pushover_app_token", ""), settings.get("pushover_user_key", ""),
                    "Racher Pager · SYSTEMTEST", "Systemtest: Pushover-kanalen virker.",
                )
                checks["pushover"] = {"status": "ok", "detail": "Test sendt"}
            except Exception as exc:
                checks["pushover"] = {"status": "failed", "detail": str(exc)[:200]}
                overall = False
        else:
            checks["pushover"] = {"status": "disabled", "detail": "Pushover er deaktiveret"}

        subscriptions = core.storage.list_user_push_subscriptions(g.user["id"])
        if subscriptions:
            sent = 0
            failed = 0
            for subscription in subscriptions:
                try:
                    core.web_push.send(subscription, {
                        "title": "Racher Pager · SYSTEMTEST",
                        "body": "Systemtest: Web Push virker på denne enhed.",
                        "url": "/",
                    })
                    sent += 1
                except WebPushException as exc:
                    failed += 1
                    if core.web_push.is_gone(exc):
                        core.storage.delete_push_subscription(subscription["endpoint"])
                except Exception:
                    failed += 1
            checks["web_push"] = {
                "status": "ok" if sent and not failed else "partial" if sent else "failed",
                "detail": f"{sent}/{len(subscriptions)} sendt",
            }
            if failed:
                overall = False
        else:
            checks["web_push"] = {"status": "disabled", "detail": "Ingen push-enhed på din konto"}

        receive_all = core.routing.user_receive_all(g.user["id"])
        station_count = len(core.routing.user_stations(g.user["id"]))
        checks["routing"] = {
            "status": "ok",
            "detail": "Alle meldinger" if receive_all else f"{station_count} område(r) valgt",
        }
        core.storage.add_audit(g.user["id"], "system-delivery-test", f"ok={1 if overall else 0}")
        return jsonify({"ok": overall, "checks": checks, "tested_at": datetime.now(timezone.utc).isoformat()})

    core.app.add_url_rule(
        "/api/system/test-delivery",
        endpoint="api_system_delivery_test",
        view_func=core.auth_required(admin=True)(system_delivery_test),
        methods=["POST"],
    )

    return ops
