from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from storage import add_event, data_dir


def _install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_root() -> Path:
    extracted = getattr(sys, "_MEIPASS", None)
    if extracted:
        return Path(extracted)
    return Path(__file__).resolve().parent


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class WhatsAppBridgeManager:
    """Own the local Node/OpenWA sidecar used by the Windows application."""

    def __init__(self) -> None:
        self.port = _free_local_port()
        self.token = secrets.token_urlsafe(32)
        self.process: subprocess.Popen | None = None
        self._log_handle = None

    @property
    def whatsapp_data_dir(self) -> Path:
        path = data_dir() / "whatsapp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def qr_path(self) -> Path:
        return self.whatsapp_data_dir / "qr.png"

    @property
    def bridge_path(self) -> Path:
        candidates = [
            _install_root() / "whatsapp" / "bridge.js",
            _resource_root() / "whatsapp" / "bridge.js",
            Path(__file__).resolve().parent / "whatsapp" / "bridge.js",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[-1]

    @property
    def node_executable(self) -> Path | None:
        override = os.getenv("SBR_PAGER_GATEWAY_NODE_EXE", "").strip()
        candidates: list[Path] = []
        if override:
            candidates.append(Path(override))
        candidates.extend(
            [
                _install_root() / "runtime" / "node" / "node.exe",
                _resource_root() / "runtime" / "node" / "node.exe",
                Path(__file__).resolve().parent / "runtime" / "node" / "node.exe",
            ]
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        system_node = shutil.which("node")
        return Path(system_node) if system_node else None

    @property
    def browser_cache_dir(self) -> Path | None:
        candidates = [
            _install_root() / "runtime" / "puppeteer-cache",
            _resource_root() / "runtime" / "puppeteer-cache",
            Path(__file__).resolve().parent / "runtime" / "puppeteer-cache",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def runtime_status(self) -> tuple[bool, str]:
        node = self.node_executable
        if node is None:
            return False, "Node-runtime mangler"
        bridge = self.bridge_path
        if not bridge.exists():
            return False, "WhatsApp bridge.js mangler"
        package = bridge.parent / "node_modules" / "@open-wa" / "wa-automate"
        if not package.exists():
            return False, "OpenWA-runtime mangler (npm install i whatsapp-mappen i udviklingsmiljø)"
        return True, f"Runtime klar · {node.name}"

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        ready, detail = self.runtime_status()
        if not ready:
            raise RuntimeError(detail)

        env = os.environ.copy()
        env.update(
            {
                "SBR_WA_PORT": str(self.port),
                "SBR_WA_TOKEN": self.token,
                "SBR_WA_SESSION_ID": "sbr-pager-gateway",
                "SBR_WA_DATA_DIR": str(self.whatsapp_data_dir),
                "NO_UPDATE_NOTIFIER": "1",
            }
        )
        browser_cache = self.browser_cache_dir
        if browser_cache is not None:
            env["PUPPETEER_CACHE_DIR"] = str(browser_cache)

        log_path = self.whatsapp_data_dir / "bridge.log"
        self._log_handle = log_path.open("a", encoding="utf-8")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.process = subprocess.Popen(
            [str(self.node_executable), str(self.bridge_path)],
            cwd=str(self.bridge_path.parent),
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        add_event("info", "whatsapp_start", "Lokal WhatsApp-motor startet")

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._log_handle:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def status(self, timeout: float = 0.8) -> dict:
        if self.process and self.process.poll() is not None:
            return {
                "state": "error",
                "detail": f"WhatsApp-motor stoppede med kode {self.process.returncode}",
                "qrAvailable": False,
            }

        try:
            result = self._request("/status", timeout=timeout)
            if isinstance(result, dict):
                return result
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass

        ready, detail = self.runtime_status()
        if not ready:
            return {"state": "runtime_missing", "detail": detail, "qrAvailable": False}
        if self.process is None:
            return {"state": "stopped", "detail": "WhatsApp-motor er stoppet", "qrAvailable": False}
        return {"state": "starting", "detail": "Starter WhatsApp-motor", "qrAvailable": False}

    def send_text(self, phone: str, text: str, timeout: float = 20) -> str | None:
        result = self._request(
            "/send",
            method="POST",
            payload={"to": phone, "text": text},
            timeout=timeout,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(str((result or {}).get("error") or "WhatsApp-afsendelse fejlede"))
        value = result.get("messageId")
        return str(value) if value is not None else None

    def logout(self) -> None:
        try:
            self._request("/logout", method="POST", payload={}, timeout=15)
        finally:
            add_event("info", "whatsapp_logout", "WhatsApp-session logget ud")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        timeout: float = 2,
    ):
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            try:
                response_payload = json.loads(details)
                reason = response_payload.get("error") or details
            except json.JSONDecodeError:
                reason = details
            raise RuntimeError(f"WhatsApp bridge HTTP {exc.code}: {reason}") from exc
