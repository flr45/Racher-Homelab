import socket
import urllib.error
from datetime import timedelta

import pytest
from bs4 import BeautifulSoup

from test_delivery import ingest
from diagnostics import internet_status as real_internet_status


@pytest.mark.parametrize('path', ['/diagnostik', '/enkelt-test', '/api/enkelt-test/status'])
def test_diagnostics_require_login(p, client, path):
    assert p.app.test_client().get(path).status_code == 302
    assert client.get(path).status_code == 200


def queue_test(p, client):
    recipient = p.base.Recipient.query.first()
    if recipient is None:
        recipient = p.base.Recipient(name='Valgt', phone='+4522222222', active=True)
        p.db.session.add(recipient);p.db.session.commit()
    page = BeautifulSoup(client.get('/enkelt-test').text, 'html.parser')
    payload={'csrf_token':'test-csrf','test_token':page.select_one('[name="test_token"]')['value'],'recipient_id':str(recipient.id)}
    assert client.post('/enkelt-test',data=payload).status_code == 302
    return payload


def test_single_test_sends_only_to_selected_person_and_records_receipt(p, client, monkeypatch):
    calls=[]
    payload=queue_test(p,client)
    p.db.session.add(p.base.Recipient(name='Anden',phone='+4533333333',active=True));p.db.session.commit()
    monkeypatch.setattr(p.base,'send_whatsapp',lambda phone,body:calls.append((phone,body)) or 'test-wa-id')
    assert not calls
    p.diagnostics.run_single_test()
    assert len(calls)==1 and calls[0][0]=='+4522222222'
    result=client.get('/api/enkelt-test/status').json['tests'][0]
    assert result['status']=='sent' and result['message_id']=='test-wa-id' and result['elapsed_ms']>=0
    # Repeat a POST after the HTTP response was lost: never queue it again.
    assert client.post('/enkelt-test',data=payload).status_code==302
    p.diagnostics.run_single_test()
    assert len(calls)==1 and p.diagnostics.SingleWhatsAppTest.query.count()==1


def test_single_test_csrf_and_target_validation(p,client):
    assert client.post('/enkelt-test',data={'recipient_id':'1'}).status_code==400
    client.get('/enkelt-test')
    with client.session_transaction() as session:token=session['single_test_token']
    assert client.post('/enkelt-test',data={'csrf_token':'test-csrf','test_token':token,'recipient_id':'bad'}).status_code==400
    assert client.post('/enkelt-test',data={'csrf_token':'test-csrf','test_token':'forged','recipient_id':'1'}).status_code==400


@pytest.mark.parametrize('change',['paused','changed','deleted','expired'])
def test_obsolete_test_is_cancelled_without_sending(p,client,monkeypatch,change):
    queue_test(p,client)
    recipient=p.base.Recipient.query.one()
    if change=='paused':recipient.active=False
    elif change=='changed':recipient.phone='+4533333333'
    elif change=='deleted':p.db.session.delete(recipient)
    else:p.diagnostics.SingleWhatsAppTest.query.one().created_at=p.base.utcnow()-timedelta(minutes=6)
    p.db.session.commit()
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *args:pytest.fail('Must not send'))
    p.diagnostics.run_single_test()
    assert p.diagnostics.SingleWhatsAppTest.query.one().status=='cancelled'


def test_interrupted_test_is_not_automatically_resent(p,client,monkeypatch):
    queue_test(p,client)
    test=p.diagnostics.SingleWhatsAppTest.query.one();test.status='running';p.db.session.commit()
    p.diagnostics.recover_inflight_tests()
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *args:pytest.fail('Must not resend'))
    p.diagnostics.run_single_test()
    assert test.status=='uncertain' and 'genstartede' in test.error


def test_failed_test_stores_error_and_does_not_retry(p,client,monkeypatch):
    queue_test(p,client)
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *args:(_ for _ in ()).throw(RuntimeError('OpenWA offline')))
    p.diagnostics.run_single_test()
    test=p.diagnostics.SingleWhatsAppTest.query.one()
    assert test.status=='failed' and test.error=='OpenWA offline'
    assert p.deliveries.queue_snapshot()['failed']==0
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *args:pytest.fail('Must not retry'))
    p.diagnostics.run_single_test()


def test_rejection_reason_survives_sender_setting_change(p,client):
    ingest(p)
    row=p.base.InboundMessage.query.one()
    assert 'ikke godkendt' in p.diagnostics.explain_inbound(row)
    client.post('/indstillinger/afsenderfilter',data={'csrf_token':'test-csrf','accept_all':'1'})
    assert 'ikke godkendt' in p.diagnostics.explain_inbound(row)


def test_no_subscribed_recipient_explains_reason_without_replay(authorized,client):
    p=authorized
    ingest(p,body='(A) Test af alarm')
    row=p.base.InboundMessage.query.one()
    assert 'valgt Test' in p.diagnostics.explain_inbound(row)
    recipient=p.base.Recipient.query.one()
    p.db.session.add(p.stations.RecipientStationFilter(recipient_id=recipient.id,station='TEST'));p.db.session.commit()
    ingest(p,body='(A) Test af alarm')
    assert p.base.WhatsAppDelivery.query.count()==0


def test_modem_noise_reason_is_not_reported_as_sender_rejection(authorized):
    p=authorized
    ingest(p,body='\x00@@@@@@@@@@@@')
    assert 'kontroltegn' in p.diagnostics.explain_inbound(p.base.InboundMessage.query.one())


def test_wan_failure_is_separate_from_online_sms_and_whatsapp(p,client,monkeypatch):
    monkeypatch.setattr(p.diagnostics,'internet_status',lambda:{'state':'offline','detail':'DNS-opslag fejlede','checked_at':p.base.utcnow()})
    result=client.get('/api/dashboard/status').json
    assert result['internet']['state']=='offline' and result['modem']['state']=='online' and result['openwa']['state']=='ready'
    assert result['good'] is False


class NetworkResponse:
    status=204
    def __init__(self,url):self.url=url
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def geturl(self):return self.url


def test_internet_check_uses_fallback_and_distinguishes_dns_failure(p,monkeypatch):
    calls=[]
    def open_request(request,timeout):
        calls.append(request)
        if len(calls)==1:raise urllib.error.URLError(socket.gaierror('DNS'))
        return NetworkResponse(request.full_url)
    opener=type('Opener',(),{'open':staticmethod(open_request)})()
    monkeypatch.setattr(p.diagnostics.urllib.request,'build_opener',lambda *args:opener)
    assert p.diagnostics.probe_internet()['state']=='online'
    assert len(calls)==2 and all(call.get_method()=='HEAD' for call in calls)
    opener.open=lambda *args,**kw:(_ for _ in ()).throw(urllib.error.URLError(socket.gaierror('DNS')))
    result=p.diagnostics.probe_internet()
    assert result['state']=='offline' and 'DNS' in result['detail']


def test_redirect_and_invalid_config_do_not_report_internet_online(p,monkeypatch):
    opener=type('Opener',(),{'open':staticmethod(lambda *args,**kw:NetworkResponse('https://login.invalid/'))})()
    monkeypatch.setattr(p.diagnostics.urllib.request,'build_opener',lambda *args:opener)
    assert p.diagnostics.probe_internet()['state']=='degraded'
    monkeypatch.setenv('SMS_WHATSAPP_INTERNET_CHECK_URLS','http://invalid/')
    assert p.diagnostics.probe_internet()['state']=='unknown'


def test_internet_probe_is_cached_for_thirty_seconds(p,monkeypatch):
    calls=[]
    monkeypatch.setattr(p.diagnostics,'probe_internet',lambda:calls.append(1) or {'state':'online'})
    monkeypatch.setattr(p.diagnostics,'_internet_cache',(0.0,{}))
    assert real_internet_status()['state']=='online'
    assert real_internet_status()['state']=='online'
    assert calls==[1]
