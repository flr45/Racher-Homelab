"""Authenticated transport for Pager Gateway -> remote SMS Gateway."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from ric_sms import RicSmsRouter, register_ric_sms_routes


class AuthenticatedRicSmsRouter(RicSmsRouter):
    """RIC SMS router with remote auth and late-duplicate awareness.

    A pager dispatch can be repeated to several RICs. For non-burst paths the
    first copy may already be delivered before a later copy carrying the RIC
    with an SMS rule arrives. Those later copies are stored as duplicates and
    never reach the normal notification hook. Wrap ingestion as well so a newly
    stored duplicate can re-evaluate the complete duplicate tree against the
    SMS rules. Deliveries are still reserved against the root message id, so the
    existing UNIQUE(message_id, recipient) constraint keeps this idempotent.
    """

    def __init__(self, core: Any) -> None:
        super().__init__(core)
        self._sms_original_ingest = core.ingest_event
        core.ingest_event = self.ingest_and_sms_duplicates

    def _duplicate_root(self, message_id: int) -> tuple[int, dict[str, Any]] | None:
        """Follow duplicate_of links to the stable root message."""
        current_id = int(message_id)
        seen: set[int] = set()
        try:
            with self.core.storage.connect() as conn:
                while current_id not in seen and len(seen) < 128:
                    seen.add(current_id)
                    row = conn.execute(
                        "SELECT * FROM messages WHERE id=?", (current_id,)
                    ).fetchone()
                    if row is None:
                        return None
                    data = dict(row)
                    parent = data.get("duplicate_of")
                    if parent is None:
                        return current_id, data
                    current_id = int(parent)
        except Exception as exc:  # noqa: BLE001
            self.core.app.logger.warning(
                "Could not resolve duplicate root for SMS message %s: %s",
                message_id,
                exc,
            )
        return None

    def _event_rics(self, message_id: int, event: dict[str, Any]) -> set[str]:
        """Return every RIC in the complete duplicate tree for this dispatch."""
        result: set[str] = set()
        if event.get("ric"):
            result.add(str(event["ric"]).strip())

        root = self._duplicate_root(message_id)
        root_id = root[0] if root else int(message_id)
        try:
            with self.core.storage.connect() as conn:
                rows = conn.execute(
                    """WITH RECURSIVE duplicate_tree(id, ric) AS (
                           SELECT id, ric FROM messages WHERE id=?
                           UNION
                           SELECT m.id, m.ric
                           FROM messages m
                           JOIN duplicate_tree d ON m.duplicate_of=d.id
                       )
                       SELECT ric FROM duplicate_tree""",
                    (root_id,),
                ).fetchall()
            result.update(
                str(row["ric"] or "").strip()
                for row in rows
                if str(row["ric"] or "").strip()
            )
        except Exception as exc:  # noqa: BLE001
            self.core.app.logger.warning(
                "Could not resolve duplicate-tree RICs for SMS message %s: %s",
                message_id,
                exc,
            )
        return result

    def _queue_from_late_duplicate(self, message_id: int) -> int:
        """Re-evaluate SMS routing when a later duplicate RIC has just arrived."""
        try:
            with self.core.storage.connect() as conn:
                row = conn.execute(
                    "SELECT duplicate_of, source FROM messages WHERE id=?",
                    (int(message_id),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            self.core.app.logger.warning(
                "Could not inspect duplicate SMS candidate %s: %s", message_id, exc
            )
            return 0

        if row is None or row["duplicate_of"] is None:
            return 0
        if not str(row["source"] or "").lower().startswith("pdl"):
            return 0

        root = self._duplicate_root(message_id)
        if root is None:
            return 0
        root_id, root_event = root
        if not bool(root_event.get("delivery_eligible")):
            return 0
        return self.queue_for_event(root_id, root_event)

    def ingest_and_sms_duplicates(self, event: Any) -> int:
        message_id = self._sms_original_ingest(event)
        try:
            self._queue_from_late_duplicate(message_id)
        except Exception as exc:  # noqa: BLE001
            self.core.app.logger.warning(
                "RIC SMS late-duplicate routing failed for message %s: %s",
                message_id,
                exc,
            )
        return message_id

    def _post_outgoing(self, gateway_url: str, recipient: str, body: str) -> dict[str, Any]:
        endpoint = gateway_url.rstrip("/") + "/api/outgoing"
        payload = json.dumps(
            {"recipient": recipient, "body": body},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = os.getenv("PAGER_SMS_GATEWAY_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        outgoing = urllib.request.Request(
            endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(outgoing, timeout=8) as response:
                raw = response.read().decode("utf-8")
                if response.status not in {200, 201, 202}:
                    raise RuntimeError(f"SMS Gateway svarede HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"SMS Gateway svarede HTTP {exc.code}: {details[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Kunne ikke kontakte SMS Gateway: {exc.reason}") from exc

        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("SMS Gateway returnerede ugyldigt JSON") from exc


def install_ric_sms(core: Any, auth_required: Callable) -> AuthenticatedRicSmsRouter:
    router = AuthenticatedRicSmsRouter(core)
    register_ric_sms_routes(core, router, auth_required)
    core.ric_sms_router = router
    return router
