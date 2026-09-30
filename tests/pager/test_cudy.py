import hashlib
import io
import urllib.parse

import pytest

from cudy_client import CudyClient, CudyError, FormPage, login_fields

# Field names match Cudy's LT300 V2 emulator. Tokens and contents are synthetic.
LOGIN = '''<form action="/cgi-bin/luci/"><input name="_csrf" value="csrf-test"><input name="token" value="token-test"><input name="salt" value="salt-test"><input name="luci_username" value="admin"><input name="luci_password" value=""></form>'''
FORM = '''<form action="/cgi-bin/luci/admin/network/gcom/atcmd"><input name="token" value="at-token"><input name="cbi.submit" value="1"><input name="cbid.atcmd.1.command" value=""></form><textarea name="cbid.atcmd.1._custom">{result}</textarea>'''
PDU = "06915404969912040A91542272306900006280107085538031A8600A442DCFE920F80364EEC8C9E9331D8466CCE9653AE8DD963FC86550BB4C0675400CD003C4012D400E"


class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self, *args):self.close()


class Router:
    def __init__(self):
        self.logged_in = False
        self.requests = []
        self.expire_once = False
    def open(self, request, timeout):
        self.requests.append(request)
        if not request.data:
            return Response((FORM.format(result="") if self.logged_in else LOGIN).encode())
        body = request.data.decode()
        if "luci_password" in body:
            expected = hashlib.sha256((hashlib.sha256(("päss"+"salt-test").encode()).hexdigest()+"token-test").encode()).hexdigest()
            assert expected in body and "päss" not in body
            self.logged_in = True
            return Response(FORM.format(result="").encode())
        assert request.headers['Content-type'].startswith('multipart/form-data; boundary=')
        assert 'name="token"\r\n\r\nat-token' in body
        if self.expire_once:
            self.expire_once = False
            self.logged_in = False
            return Response(LOGIN.encode())
        return Response(FORM.format(result="\r\n+CSQ: 21,99\r\n\r\nOK\r\n").encode())


def test_official_form_login_challenge_and_session_expiry():
    router = Router()
    client = CudyClient("http://192.168.10.1", password="päss", opener=router)
    assert "+CSQ: 21" in client.command("AT+CSQ")
    router.expire_once = True
    assert "+CSQ: 21" in client.command("AT+CSQ")
    assert len([req for req in router.requests if req.data and b'luci_password' in req.data]) == 2


def test_unknown_firmware_fails_before_any_at_command():
    router = Router(); router.logged_in = True
    router.open = lambda req, timeout: Response(b"<h1>Different firmware</h1>")
    with pytest.raises(CudyError, match="genkendes ikke"):
        CudyClient("http://192.168.10.1", password="secret", opener=router).connect()


def test_probe_does_not_report_success_if_sms_storage_is_unsupported(monkeypatch):
    client = CudyClient("http://192.168.10.1")
    monkeypatch.setattr(client, "connect", lambda: client)
    monkeypatch.setattr(client, "command", lambda value: "ERROR\n" if value == "AT+CPMS?" else "OK\n")
    with pytest.raises(CudyError, match="AT-test blev afvist"):
        client.probe()


@pytest.mark.parametrize("command", ["AT+CFUN=0", "AT+CFUN=1,1", "AT+CPMS=\"ME\"", "AT+CMGD=1,4", "AT+CMGL=4\rAT+CFUN=0"])
def test_network_reset_and_bulk_delete_commands_are_blocked(command):
    with pytest.raises(CudyError, match="ikke tilladt"):
        CudyClient("http://192.168.10.1").command(command)


def test_credentials_are_never_posted_to_external_form_action():
    router = Router(); router.logged_in = False
    router.open = lambda req, timeout: Response(LOGIN.replace('/cgi-bin/luci/', 'http://other.invalid/').encode())
    with pytest.raises(CudyError, match="formularadresse"):
        CudyClient("http://192.168.10.1", password="secret", opener=router).connect()


def test_read_inbox_reassembles_danish_text_and_restores_router_mode(monkeypatch):
    import cudy_reader
    commands = []
    def command(value):
        commands.append(value)
        return '+CMGF: 1\nOK\n' if value == 'AT+CMGF?' else '+CMGL: 4,0,,40\n' + PDU + '\nOK\n' if value == 'AT+CMGL=4' else 'OK\n'
    router = type('Router', (), {'command': staticmethod(command)})()
    result = cudy_reader.read_inbox(router)
    assert result[0]['body'] == '(A) Test på færdigt høstet område med æ ø å Æ Ø Å'
    assert commands == ['AT+CMGF?', 'AT+CMGF=0', 'AT+CMGL=4', 'AT+CMGF=1']


def test_sms_is_retained_if_pager_does_not_acknowledge(monkeypatch):
    import cudy_reader
    commands = []
    router = type('Router', (), {'command': staticmethod(lambda value: commands.append(value) or 'OK\n')})()
    monkeypatch.setattr(cudy_reader.pager, 'post_message', lambda message: (_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError):
        cudy_reader.import_message(router, {'indices':[1], 'sender':'+4512345678'})
    assert not commands


def test_sms_is_deleted_only_after_both_applications_acknowledge(monkeypatch):
    import cudy_reader
    commands = []
    router = type('Router', (), {'command': staticmethod(lambda value: commands.append(value) or 'OK\n')})()
    monkeypatch.setattr(cudy_reader.pager, 'post_message', lambda message: {'accepted':True})
    monkeypatch.setattr(cudy_reader.reader, 'write_status', lambda **kwargs: None)
    cudy_reader.import_message(router, {'indices':[1,2], 'sender':'+4512345678'})
    assert commands == ['AT+CMGD=1', 'AT+CMGD=2']


@pytest.mark.parametrize("response,expected", [("+CEREG: 0,1\nOK",True),("+CREG: 0,5\nOK",True),("+CGREG: 0,2\nOK",False)])
def test_lte_and_usb_network_registration(response, expected):
    from cudy_reader import registered
    assert registered(response) is expected


def test_mode_restored_even_if_inbox_read_fails():
    import cudy_reader
    calls = []
    def command(value):
        calls.append(value)
        if value == 'AT+CMGF?':return '+CMGF: 1\nOK'
        if value == 'AT+CMGL=4':raise CudyError('timeout')
        return 'OK'
    router = type('Router', (), {'command':staticmethod(command)})()
    with pytest.raises(CudyError):cudy_reader.read_inbox(router)
    assert calls[-1] == 'AT+CMGF=1'
