import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMP = tempfile.TemporaryDirectory()
os.environ.update(DATABASE_URL="sqlite:///" + TEMP.name + "/pager.db",
                  SMS_WHATSAPP_RETRY_WORKER="false", SBR_PAGER_AUTO_GEOCODE="false",
                  SMS_WHATSAPP_ASYNC_DELIVERY="true", SMS_WHATSAPP_INGEST_TOKEN="test-ingest",
                  SMS_WHATSAPP_ADMIN_PASSWORD="test-adgangskode-æ", SMS_WHATSAPP_SESSION_SECRET="test-session-secret",
                  SBR_PAGER_PARENT_STATE_FILE=TEMP.name + "/parents.json")
sys.path.insert(0, str(ROOT / "services/sms-whatsapp"))
import dashboard_app as pager

os.environ.update(DATABASE_URL="sqlite:///" + TEMP.name + "/gateway.db",
                  SMS_GATEWAY_API_TOKEN="test-gateway", VAGTBYTTE_FORWARD_ENABLED="false",
                  GATEWAY_STATUS_FILE=TEMP.name + "/gateway-status.json",
                  MODEM_STATUS_FILE=TEMP.name + "/modem-status.json", RACHER_MONITOR_SMS_TO="+4512345678")
os.environ["SMS_MULTIPART_STATUS_FILE"] = TEMP.name + "/multipart-status.json"
sys.path.insert(0, str(ROOT / "services/sms-gateway"))
spec = importlib.util.spec_from_file_location("base_app", ROOT / "services/sms-gateway/app.py")
gateway_base = importlib.util.module_from_spec(spec)
sys.modules["base_app"] = gateway_base
spec.loader.exec_module(gateway_base)
import queued_app as gateway


@pytest.fixture()
def p(monkeypatch):
    with pager.app.app_context():
        pager.db.drop_all()
        pager.db.create_all()
        monkeypatch.setattr(pager.base, "openwa_status", lambda: {"state": "ready", "detail": "SBR-PAGER"})
        monkeypatch.setattr(pager.diagnostics, "internet_status", lambda: {"state": "online", "detail": "Testnetværk", "checked_at": pager.base.utcnow()})
        monkeypatch.setattr(pager, "gateway_status", lambda: {"modem": {"state": "online", "transport": "usb"}, "gateway": {}})
        yield pager
        pager.db.session.remove()


@pytest.fixture()
def g(monkeypatch):
    monkeypatch.setenv("SMS_MODEM_DRIVER", "usb")
    with gateway.app.app_context():
        gateway.db.drop_all()
        gateway.db.create_all()
        yield gateway
        gateway.db.session.remove()


@pytest.fixture()
def authorized(p):
    p.db.session.add_all([p.base.AllowedSender(name="Alarmcentral", phone="+4512345678", active=True),
                          p.base.Recipient(name="Modtager", phone="+4522222222", active=True)])
    p.db.session.commit()
    return p


@pytest.fixture()
def client(p):
    c = p.app.test_client()
    with c.session_transaction() as s:
        s["admin_authenticated"] = True
        s["csrf_token"] = "test-csrf"
    return c
