"""Authenticated transport for Pager Gateway -> remote SMS Gateway."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from ric_sms import RicSmsRouter, register_ric_sms_routes


class AuthenticatedRicSmsRouter(RicSmsRouter):
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
