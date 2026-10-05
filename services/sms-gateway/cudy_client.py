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
        self.document = document
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.form = {"action": attrs.get("action", ""), "fields": {}}
            self.forms.append(self.form)
        elif tag == "input" and self.form is not None and attrs.get("name"):
            self.form["fields"][attrs["name"]] = attrs.get("value", "")
        elif tag == "button" and self.form is not None and attrs.get("name"):
            self.form.setdefault("buttons", {})[attrs["name"]] = attrs.get("value", "")
        elif tag == "textarea":
            self.textarea = attrs.get("name") or attrs.get("id")
            if self.textarea:
                self.textareas[self.textarea] = ""
                if self.form is not None:
                    self.form.setdefault("textareas", set()).add(self.textarea)

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

    def prepare_sms(self, recipient: str, body: str):
        """Validate and prepare the observed LT300 V3 SMS form.

        The returned token-bearing fields are private and must never be logged.
        """
        if not isinstance(recipient, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", recipient):
            raise CudyError("Modtager skal være ét telefonnummer med landekode")
        if not isinstance(body, str) or not body.strip() or len(body) > 160:
            raise CudyError("SMS skal indeholde 1–160 tegn")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in body):
            raise CudyError("SMS-teksten indeholder ugyldige kontroltegn")
        url = self.root + "admin/network/gcom/sms/smsnew?nomodal=&iface=4g"
        page = self._request(url)
        if page.containing("luci_password"):
            self._login(page)
            page = self._request(url)
        phone = "cbid.smsnew.1.phone"
        content = "cbid.smsnew.1.content"
        send = "cbid.smsnew.1.send"
        form = page.containing(phone)
        if (not form or not form["fields"].get("token")
                or content not in form.get("textareas", set())
                or send not in form.get("buttons", {})):
            raise CudyError("Routerens SMS-formular genkendes ikke; intet sendt")
        action = self._same_origin(urllib.parse.urljoin(url, form["action"] or url))
        if urllib.parse.urlsplit(action).path != urllib.parse.urlsplit(url).path:
            raise CudyError("Ukendt SMS-formularadresse; intet sendt")
        # Copy only the observed safe controls, never unrelated configuration.
        fields = {key: form["fields"][key] for key in ("token", "_csrf") if key in form["fields"]}
        fields.update({"cbi.submit": "1", "timeclock": str(int(time.time())),
                       phone: recipient, content: body, send: form["buttons"][send]})
        multipart_form(fields)  # Validate encoding before a future explicit send.
        return action, fields

    def _authenticated_page(self, url: str):
        page = self._request(url)
        if page.containing("luci_password"):
            self._login(page)
            page = self._request(url)
        return page

    def sms_enabled(self) -> bool:
        """Return the stock-firmware SMS service state without changing it."""
        url = self.root + "admin/network/gcom/config/sms"
        page = self._authenticated_page(url)
        form = page.containing("cbid.sms.4g.enabled")
        if not form or not form["fields"].get("token"):
            raise CudyError("Routerens SMS Enable-formular genkendes ikke")
        value = str(form["fields"].get("cbid.sms.4g.enabled", "")).strip()
        if value not in {"0", "1"}:
            raise CudyError("Routerens SMS Enable-status er ukendt")
        return value == "1"

    def set_sms_enabled(self, enabled: bool) -> bool:
        """Set SMS Enable through the observed LuCI form and verify the result."""
        url = self.root + "admin/network/gcom/config/sms"
        page = self._authenticated_page(url)
        form = page.containing("cbid.sms.4g.enabled")
        if not form or not form["fields"].get("token"):
            raise CudyError("Routerens SMS Enable-formular genkendes ikke")

        fields = dict(form["fields"])
        fields["cbi.submit"] = "1"
        fields["cbi.cbe.sms.4g.enabled"] = "1"
        fields["cbid.sms.4g.enabled"] = "1" if enabled else "0"
        if "cbi.apply" in form.get("buttons", {}):
            fields["cbi.apply"] = form["buttons"]["cbi.apply"]

        action = self._same_origin(
            urllib.parse.urljoin(url, form["action"] or url)
        )
        if urllib.parse.urlsplit(action).path != urllib.parse.urlsplit(url).path:
            raise CudyError("Ukendt SMS Enable-formularadresse")

        self._request(action, fields)
        time.sleep(1)
        current = self.sms_enabled()
        if current is not bool(enabled):
            raise CudyError(
                "Cudy bekræftede ikke ændringen af SMS Enable-status"
            )
        return current

    def ensure_sms_enabled(self) -> bool:
        """Keep Cudy's own SMS engine enabled for unattended send/receive.

        The LT300 can store a web Outbox entry while its SMS engine is disabled.
        SBR Pager therefore treats SMS Enable as a required router service, not
        as a per-message switch. If the router reboots or the setting is changed,
        the reader can safely self-heal it back to enabled.
        """
        if self.sms_enabled():
            return True
        return self.set_sms_enabled(True)

    def outbox_ids(self) -> list[str]:
        """Read stable cfg IDs from Cudy's stored/sent SMS list."""
        url = self.root + "admin/network/gcom/sms/smslist?smsbox=sto&iface=4g"
        page = self._authenticated_page(url)
        ids = re.findall(
            r'/sms/readsms"\s*,\s*"iface=4g&cfg=([A-Za-z0-9_-]+)&smsbox=sto"',
            page.document,
            flags=re.IGNORECASE,
        )
        return list(dict.fromkeys(ids))

    def outbox_message(self, cfg: str) -> dict:
        """Read one Outbox item using the same endpoint as 'More Details'."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cfg or ""):
            raise CudyError("Ugyldigt Cudy Outbox-id")

        query = urllib.parse.urlencode(
            {"iface": "4g", "cfg": cfg, "smsbox": "sto"}
        )
        url = self.root + "admin/network/gcom/sms/readsms?" + query
        page = self._authenticated_page(url)
        form = page.containing("cbid.smsread.1.phone")
        if not form:
            raise CudyError("Routerens Outbox-detaljer genkendes ikke")

        recipient = str(
            form["fields"].get("cbid.smsread.1.phone", "")
        ).strip()
        body = page.textareas.get("cbid.smsread.1.content")
        if body is None and page.textareas:
            body = next(iter(page.textareas.values()))
        body = (body or "").strip()

        return {"cfg": cfg, "recipient": recipient, "body": body}

    def find_outbox_message(self, recipient: str, body: str) -> dict | None:
        """Find an exact previously accepted Outbox item."""
        for cfg in self.outbox_ids():
            try:
                item = self.outbox_message(cfg)
            except CudyError:
                continue
            if (
                item["recipient"] == recipient
                and item["body"].strip() == body.strip()
            ):
                return item
        return None

    def send_sms(
        self,
        recipient: str,
        body: str,
        *,
        confirm_seconds: float = 12.0,
        poll_seconds: float = 0.5,
        settle_seconds: float | None = None,
    ) -> dict:
        """Send one SMS while keeping Cudy's SMS engine permanently enabled.

        Cudy's Outbox timestamp is the queue time, not a delivery report. A new
        cfg ID matching recipient and body is therefore treated as router
        acceptance, not handset delivery.
        """
        # Validate before changing router state.
        if not isinstance(recipient, str) or not re.fullmatch(
            r"\+[1-9][0-9]{7,14}", recipient
        ):
            raise CudyError("Modtager skal være ét telefonnummer med landekode")
        if not isinstance(body, str) or not body.strip() or len(body) > 160:
            raise CudyError("SMS skal indeholde 1–160 tegn")

        before = set(self.outbox_ids())
        accepted_cfg = None
        submission_error = None

        # SMS Enable is a permanent service prerequisite. Never turn it off
        # after a send: the physical LT300 test proved that disabling it can
        # leave messages in Cudy Outbox without transmitting them.
        self.ensure_sms_enabled()

        # Fetch a fresh SMS form after checking router configuration so its
        # CSRF/token state cannot be stale.
        action, fields = self.prepare_sms(recipient, body)
        try:
            self._request(action, fields)
        except Exception as exc:  # noqa: BLE001
            # A transport failure may happen after Cudy accepted the POST.
            # Reconcile Outbox before deciding whether the send failed.
            submission_error = exc

        deadline = time.monotonic() + max(1.0, float(confirm_seconds))
        interval = max(0.1, float(poll_seconds))
        while True:
            for cfg in self.outbox_ids():
                if cfg in before:
                    continue
                try:
                    item = self.outbox_message(cfg)
                except CudyError:
                    continue
                if (
                    item["recipient"] == recipient
                    and item["body"].strip() == body.strip()
                ):
                    accepted_cfg = cfg
                    break
            if accepted_cfg or time.monotonic() >= deadline:
                break
            time.sleep(interval)

        # Retain the parameter for compatibility with older callers, but there
        # is deliberately no post-send disable/drain cycle anymore.
        _ = settle_seconds

        if accepted_cfg:
            return {
                "cfg": accepted_cfg,
                "accepted": True,
                "sms_engine_enabled": True,
                "restore_error": None,
            }

        if submission_error is not None:
            raise CudyError(
                "Cudy-afsendelsen er usikker: formular-kaldet fejlede og "
                "ingen ny matchende Outbox-post blev fundet"
            ) from submission_error

        raise CudyError(
            "Cudy kvitterede ikke med en ny matchende Outbox-post inden timeout"
        )

    def probe(self):
        self.connect()
        send_enabled = os.getenv("CUDY_SMS_SEND_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        result = {"transport": "cudy", "capability": "send-receive" if send_enabled else "receive-only", "at": self.command("AT")}
        for name, command in [("sim", "AT+CPIN?"), ("network", "AT+CEREG?"), ("signal", "AT+CSQ"), ("storage", "AT+CPMS?")]:
            result[name] = self.command(command)
        if "ERROR" in result["network"]:
            result["network"] = self.command("AT+CREG?")
        if any("ERROR" in result[name] for name in ("at", "sim", "network", "signal", "storage")):
            raise CudyError("Cudys AT-test blev afvist; kontrollér firmwareens SMS- og AT-understøttelse")
        return result