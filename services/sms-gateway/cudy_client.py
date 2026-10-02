"""Cudy stock-firmware AT form transport, based on the official LT300 emulator.

This is a version-dependent web interface, not an advertised vendor SMS API.
Unknown forms and login challenges fail closed. No radio, APN or router reset
commands are used. Physical LT300 firmware still requires commissioning.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser


class CudyError(RuntimeError):
    pass


class FormPage(HTMLParser):
    def __init__(self, document: str):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.form = None
        self.textareas = {}
        self.textarea = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.form = {"action": attrs.get("action", ""), "fields": {}}
            self.forms.append(self.form)
        elif tag == "input" and self.form is not None and attrs.get("name"):
            self.form["fields"][attrs["name"]] = attrs.get("value", "")
        elif tag == "textarea":
            self.textarea = attrs.get("name") or attrs.get("id")
            if self.textarea:
                self.textareas[self.textarea] = ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None
        elif tag == "textarea":
            self.textarea = None

    def handle_data(self, data):
        if self.textarea:
            self.textareas[self.textarea] += data

    def containing(self, field):
        return next((form for form in self.forms if field in form["fields"]), None)


def login_fields(form: dict, username: str, password: str) -> dict:
    """Match sysauth.js: SHA256(SHA256(password + salt) + token)."""
    fields = dict(form["fields"])
    salt = fields.get("salt")
    if salt is not None:
        password = hashlib.sha256((password + salt).encode()).hexdigest()
        if "token" in fields:
            password = hashlib.sha256((password + fields["token"]).encode()).hexdigest()
    fields.update(luci_username=username, luci_password=password,
                  zonename="Europe/Copenhagen", timeclock=str(int(time.time())), luci_language="en")
    return fields


def multipart_form(fields: dict) -> tuple[bytes, str]:
    boundary = "----sbr" + os.urandom(16).hex()
    chunks = []
    for key, value in fields.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", key):
            raise CudyError("Ukendt felt i routerens formular")
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise CudyError("Routeren forsøgte at omdirigere til en anden adresse")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CudyClient:
    COMMAND_FIELD = "cbid.atcmd.1.command"
    RESULT_FIELD = "cbid.atcmd.1._custom"
    _SAFE_AT = re.compile(r'^AT(?:\+(?:CPIN|CREG|CEREG|CGREG|CPMS|CMGF)\?|\+CSQ|\+CMGF=[01]|\+CMGL=4|\+CMGD=\d+)?$')

    def __init__(self, base_url=None, username=None, password=None, timeout=None, opener=None):
        self.base_url = (base_url or os.getenv("CUDY_BASE_URL", "http://192.168.10.1")).rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CudyError("CUDY_BASE_URL skal være routerens HTTP(S)-adresse uden loginoplysninger")
        self.username = username or os.getenv("CUDY_USERNAME", "admin")
        self.password = password if password is not None else os.getenv("CUDY_PASSWORD", "")
        self.timeout = float(timeout or os.getenv("CUDY_HTTP_TIMEOUT_SECONDS", "15"))
        self.root = self.base_url + "/cgi-bin/luci/"
        self.at_url = self.root + "admin/network/gcom/atcmd"
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            SameOriginRedirect(),
        )
        self.at_form = None

    def _same_origin(self, url):
        current, target = urllib.parse.urlsplit(self.base_url), urllib.parse.urlsplit(url)
        if (current.scheme, current.netloc) != (target.scheme, target.netloc):
            raise CudyError("Ukendt formularadresse fra routeren")
        return url

    def _request(self, url, fields=None):
        url = self._same_origin(url)
        data, content_type = multipart_form(fields) if fields is not None else (None, None)
        headers = {"Accept": "text/html", "User-Agent": "SBR-Pager-Cudy/1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=self.timeout) as response:
                return FormPage(response.read(512 * 1024).decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            # LT300 V3 firmware serves its login challenge with HTTP 403.
            # Only recognize a real login form; other HTTP failures stay errors.
            with exc:
                if exc.code in (401, 403):
                    page = FormPage(exc.read(512 * 1024).decode("utf-8", errors="replace"))
                    if page.containing("luci_password"):
                        return page
            raise CudyError(f"Cudy svarede HTTP {exc.code}; kontrollér adresse, login og firmware") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CudyError("Cudy kunne ikke kontaktes; kontrollér LAN-forbindelse og routeradresse") from None

    def _login(self, page):
        form = page.containing("luci_password")
        if not form or not self.password:
            raise CudyError("Cudy kræver login; konfigurér CUDY_PASSWORD")
        action = urllib.parse.urljoin(self.root, form["action"] or self.root)
        self._request(action, login_fields(form, self.username, self.password))
        page = self._request(self.at_url)
        if page.containing("luci_password"):
            raise CudyError("Cudy-login blev afvist; kontrollér adgangskoden")
        return page

    def connect(self):
        try:
            page = self._request(self.at_url)
        except CudyError:
            page = self._request(self.root)
        if page.containing("luci_password"):
            page = self._login(page)
        form = page.containing(self.COMMAND_FIELD)
        if not form:
            page = self._request(self.at_url)
            form = page.containing(self.COMMAND_FIELD)
        if not form or not form["fields"].get("token"):
            raise CudyError("Firmwareens AT-formular genkendes ikke; behold USB-modemmet indtil tilpasning")
        self.at_form = form
        return self

    def command(self, command: str):
        if not self._SAFE_AT.fullmatch(command):
            raise CudyError("Denne AT-kommando er ikke tilladt i Cudy-adapteren")
        if self.at_form is None:
            self.connect()
        fields = dict(self.at_form["fields"])
        fields.update({self.COMMAND_FIELD: command, "cbi.submit": "1", "timeclock": str(int(time.time()))})
        action = urllib.parse.urljoin(self.at_url, self.at_form["action"] or self.at_url)
        page = self._request(action, fields)
        if page.containing("luci_password"):
            # The login form proves the command was not accepted. Retry once.
            self.at_form = None
            self.connect()
            fields = dict(self.at_form["fields"])
            fields.update({self.COMMAND_FIELD: command, "cbi.submit": "1", "timeclock": str(int(time.time()))})
            page = self._request(urllib.parse.urljoin(self.at_url, self.at_form["action"] or self.at_url), fields)
        form = page.containing(self.COMMAND_FIELD)
        if form:
            self.at_form = form
        result = page.textareas.get(self.RESULT_FIELD)
        if not result or not re.search(r"(?:^|\n)\s*(?:OK|ERROR|\+CM[ES] ERROR:.*)\s*(?:\n|$)", result.replace("\r", "")):
            self.at_form = None
            raise CudyError("Cudy gav ikke et genkendeligt AT-svar; SMS beholdes på routeren")
        return result

    def probe(self):
        self.connect()
        result = {"transport": "cudy", "capability": "receive-only", "at": self.command("AT")}
        for name, command in [("sim", "AT+CPIN?"), ("network", "AT+CEREG?"), ("signal", "AT+CSQ"), ("storage", "AT+CPMS?")]:
            result[name] = self.command(command)
        if "ERROR" in result["network"]:
            result["network"] = self.command("AT+CREG?")
        if any("ERROR" in result[name] for name in ("at", "sim", "network", "signal", "storage")):
            raise CudyError("Cudys AT-test blev afvist; kontrollér firmwareens SMS- og AT-understøttelse")
        return result
