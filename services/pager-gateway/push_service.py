from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush


class WebPushService:
    def __init__(self, data_dir: Path, subject_getter) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.private_key_path = self.data_dir / "vapid-private.pem"
        self.subject_getter = subject_getter
        self._ensure_key()
        self.public_key = self._derive_public_key()

    def _ensure_key(self) -> None:
        if self.private_key_path.exists():
            return
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        tmp = self.private_key_path.with_suffix(".tmp")
        tmp.write_bytes(pem)
        os.chmod(tmp, 0o600)
        tmp.replace(self.private_key_path)

    def _derive_public_key(self) -> str:
        key = serialization.load_pem_private_key(
            self.private_key_path.read_bytes(), password=None
        )
        public_bytes = key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")

    @staticmethod
    def subscription_info(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }

    def send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> None:
        webpush(
            subscription_info=self.subscription_info(subscription),
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=str(self.private_key_path),
            vapid_claims={"sub": self.subject_getter()},
            ttl=300,
            timeout=10,
        )

    @staticmethod
    def is_gone(exc: WebPushException) -> bool:
        response = getattr(exc, "response", None)
        return bool(response is not None and response.status_code in {404, 410})
