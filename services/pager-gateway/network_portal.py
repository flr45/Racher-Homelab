#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import html
import os
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


WIFI_IFACE = os.getenv("PAGER_WIFI_IFACE", "wlan0")
HOTSPOT_CONNECTION = os.getenv("PAGER_HOTSPOT_CONNECTION", "Racher-Pager-Setup")
HOTSPOT_PASSWORD = os.getenv("PAGER_HOTSPOT_PASSWORD", "")
BIND_IP = os.getenv("PAGER_HOTSPOT_IP", "10.42.0.1")
PORT = int(os.getenv("PAGER_HOTSPOT_PORTAL_PORT", "80"))


def run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def profile_name(ssid: str) -> str:
    return "racher-wifi-" + hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:10]


def install_wifi(ssid: str, password: str) -> tuple[bool, str, str]:
    ssid = ssid.strip()
    if not 1 <= len(ssid) <= 32:
        return False, "SSID skal være 1-32 tegn.", ""
    if not 8 <= len(password) <= 63:
        return False, "Wi-Fi-adgangskoden skal være 8-63 tegn.", ""

    profile = profile_name(ssid)
    existing = run(["nmcli", "-g", "NAME", "connection", "show", profile], timeout=5)
    if existing.returncode == 0:
        run(["nmcli", "connection", "delete", profile], timeout=10)

    created = run([
        "nmcli", "connection", "add", "type", "wifi", "ifname", WIFI_IFACE,
        "con-name", profile, "ssid", ssid,
    ], timeout=15)
    if created.returncode != 0:
        return False, (created.stderr or created.stdout or "Kunne ikke oprette profil.").strip(), ""

    modified = run([
        "nmcli", "connection", "modify", profile,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "100",
        "ipv4.method", "auto",
        "ipv6.method", "auto",
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password,
    ], timeout=15)
    if modified.returncode != 0:
        run(["nmcli", "connection", "delete", profile], timeout=10)
        return False, (modified.stderr or modified.stdout or "Kunne ikke gemme profil.").strip(), ""

    return True, "Wi-Fi er gemt. Pi'en skifter netværk om et øjeblik.", profile


def switch_to_profile(profile: str) -> None:
    time.sleep(2)
    run(["nmcli", "connection", "down", HOTSPOT_CONNECTION], timeout=10)
    run(["nmcli", "connection", "up", profile], timeout=40)


def page(message: str = "", error: bool = False) -> bytes:
    notice = ""
    if message:
        cls = "error" if error else "ok"
        notice = f'<div class="{cls}">{html.escape(message)}</div>'
    body = f"""<!doctype html>
<html lang="da"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Racher Pager · Wi-Fi setup</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b0d10;color:#f4f6f8;margin:0;padding:24px}}
main{{max-width:460px;margin:40px auto;background:#12161c;border:1px solid #272e38;border-radius:18px;padding:24px}}
small,p{{color:#8e98a6;line-height:1.5}}label{{display:grid;gap:7px;margin:16px 0;color:#aab2bd}}
input{{font:inherit;padding:13px;border-radius:10px;border:1px solid #323a46;background:#0d1116;color:#fff}}
button{{width:100%;font:inherit;font-weight:700;padding:13px;border-radius:10px;border:0;background:#f4f6f8;color:#0b0d10}}
.ok,.error{{padding:12px;border-radius:10px;margin:16px 0}}.ok{{background:#173521}}.error{{background:#3b1717}}
</style></head><body><main><small>RACHER PAGER</small><h1>Nyt Wi-Fi</h1>
<p>Denne side er kun tilgængelig, når Pi'en kører sit fallback-netværk. Indtast netværket på den nye lokation.</p>
{notice}
<form method="post"><label>Wi-Fi navn (SSID)<input name="ssid" maxlength="32" required autocomplete="off"></label>
<label>Wi-Fi adgangskode<input name="password" type="password" minlength="8" maxlength="63" required autocomplete="new-password"></label>
<label>Setup PIN<input name="pin" type="password" required autocomplete="off"></label>
<button type="submit">Gem og forbind</button></form>
<p><small>Når forbindelsen skifter, forsvinder Racher-Pager-Setup. Forbind din telefon til det normale netværk igen.</small></p>
</main></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "RacherPagerSetup/1.0"

    def _send(self, data: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._send(page())

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
        except ValueError:
            length = 0
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True)
        ssid = (form.get("ssid") or [""])[0]
        password = (form.get("password") or [""])[0]
        pin = (form.get("pin") or [""])[0]
        if not HOTSPOT_PASSWORD or pin != HOTSPOT_PASSWORD:
            self._send(page("Forkert setup PIN.", True), HTTPStatus.FORBIDDEN)
            return
        ok, message, profile = install_wifi(ssid, password)
        if not ok:
            self._send(page(message, True), HTTPStatus.BAD_REQUEST)
            return
        self._send(page(message, False))
        threading.Thread(target=switch_to_profile, args=(profile,), daemon=True).start()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"network-portal: {self.address_string()} {fmt % args}", flush=True)


def wait_for_hotspot_ip() -> None:
    while True:
        result = run(["ip", "-4", "addr", "show", "dev", WIFI_IFACE], timeout=4)
        if result.returncode == 0 and BIND_IP in (result.stdout or ""):
            return
        time.sleep(3)


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("network_portal.py skal køre som root via systemd")
    while True:
        wait_for_hotspot_ip()
        try:
            server = ThreadingHTTPServer((BIND_IP, PORT), Handler)
            print(f"Racher Pager setup portal lytter på http://{BIND_IP}:{PORT}", flush=True)
            server.serve_forever(poll_interval=1)
        except OSError as exc:
            print(f"Setup portal venter: {exc}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
