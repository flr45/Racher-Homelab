import io
import json
from datetime import timedelta
from pathlib import Path
import sqlite3
import zipfile

import pytest
from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import generate_password_hash
import operator_accounts as accounts
import resilience as monitor
import offsite_backup as offsite
import encrypted_backup as encrypted

PASSWORD='Personligt-testlogin-2026'


def add_operator(p, username='freddy', role='admin'):
    row=accounts.OperatorAccount(username=username,role=role,password_hash=generate_password_hash(PASSWORD))
    p.db.session.add(row);p.db.session.commit()
    return row


def personal_client(p,row):
    client=p.app.test_client()
    client.get('/login')
    with client.session_transaction() as s: token=s['csrf_token']
    response=client.post('/login',data={'csrf_token':token,'username':row.username,'password':PASSWORD})
    assert response.status_code==302
    return client


def test_personal_login_hash_audit_and_old_cookie_rejection(authorized,client):
    p=authorized
    assert client.post('/administratorer',data={'csrf_token':'test-csrf','username':'Freddy','role':'admin','password':PASSWORD}).status_code==302
    row=accounts.OperatorAccount.query.one()
    assert row.username=='freddy' and row.password_hash.startswith('scrypt:') and PASSWORD not in row.password_hash
    person=personal_client(p,row)
    with person.session_transaction() as s: token=s['csrf_token']
    recipient=p.base.Recipient.query.one()
    assert person.post(f'/brugere/{recipient.id}/rediger',data={'csrf_token':token,'name':'Ændret personligt','phone':recipient.phone,'active':'1'}).status_code==302
    assert p.operations.AuditEntry.query.order_by(p.operations.AuditEntry.id.desc()).first().actor=='freddy'
    assert PASSWORD not in ''.join(x.changes for x in p.operations.AuditEntry.query.all())
    legacy=p.app.test_client()
    with legacy.session_transaction() as s:s['admin_authenticated']=True
    assert legacy.get('/').status_code==302


@pytest.mark.parametrize('path',['/administratorer','/administratorer/1','/modtageroversigt','/drift/backup','/drift/indstillinger','/indkoering/cudy/short','/api/enkelt-test','/api/incoming','/indstillinger/afsenderfilter','/driftsvaern','/driftsvaern/ekstern-backup'])
def test_readonly_cannot_mutate_even_with_valid_csrf_and_ingest_token(p,path):
    row=add_operator(p,role='viewer');client=personal_client(p,row)
    with client.session_transaction() as s: token=s['csrf_token']
    assert client.post(path,data={'csrf_token':token},headers={'Authorization':'Bearer test-ingest'}).status_code==403
    assert p.base.InboundMessage.query.count()==0 and monitor.OperatingIncident.query.count()==0


@pytest.mark.parametrize('path',['/','/beskeder','/modtageroversigt','/driftsvaern','/forbindelseshistorik'])
def test_readonly_can_read_and_ui_marks_role(p,path):
    client=personal_client(p,add_operator(p,role='viewer'))
    response=client.get(path)
    assert response.status_code==200 and b'data-readonly="true"' in response.data
    assert client.get('/administratorer').status_code==403


def test_account_changes_revoke_session_and_preserve_last_admin(p,client):
    row=add_operator(p);person=personal_client(p,row)
    update={'csrf_token':'test-csrf','username':'freddy','role':'viewer','active':'1','password':''}
    assert client.post('/administratorer/'+str(row.id),data=update).status_code==409
    add_operator(p,'backupadmin')
    assert client.post('/administratorer/'+str(row.id),data=update).status_code==302
    assert person.get('/').status_code==302
    new=personal_client(p,row)
    assert new.post('/drift/backup').status_code==403


@pytest.mark.parametrize('name,password,role',[('admin',PASSWORD,'admin'),('bad name',PASSWORD,'admin'),('freddy','short','admin'),('freddy',PASSWORD,'root')])
def test_invalid_accounts_are_atomic(client,p,name,password,role):
    assert client.post('/administratorer',data={'csrf_token':'test-csrf','username':name,'password':password,'role':role}).status_code==400
    assert accounts.OperatorAccount.query.count()==0


def test_login_rate_limit_csrf_environment_rotation_and_restore_epoch(p,client,monkeypatch):
    anonymous=p.app.test_client();anonymous.get('/login')
    with anonymous.session_transaction() as s:token=s['csrf_token']
    assert anonymous.post('/login',data={'username':'admin','password':'bad'}).status_code==400
    for _ in range(8): assert anonymous.post('/login',data={'csrf_token':token,'username':'admin','password':'bad'}).status_code==200
    assert anonymous.post('/login',data={'csrf_token':token,'username':'admin','password':'bad'}).status_code==429
    assert accounts.LoginFailure.query.count()==8
    monkeypatch.setenv('SMS_WHATSAPP_ADMIN_PASSWORD','rotated-environment-password')
    assert client.get('/').status_code==302
    person=personal_client(p,add_operator(p))
    p.operations.set_setting('operator_auth_epoch','new-restore-generation');p.db.session.commit()
    assert person.get('/').status_code==302


def test_login_rejects_backslash_redirect(p):
    add_operator(p);c=p.app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf_token']
    response=c.post('/login?next=/\\evil.example',data={'csrf_token':token,'username':'freddy','password':PASSWORD})
    assert response.location=='/'


def test_monitor_records_only_transitions_storage_and_paused_queue(p,monkeypatch):
    gateway={'modem':{'state':'online','transport':'usb','storage':'+CPMS: "SM",9,10,"SM",9,10'}}
    monkeypatch.setattr(p,'gateway_status',lambda:gateway)
    p.operations.set_setting('operation_mode','maintenance');p.operations.set_setting('operation_until',(p.base.utcnow()+timedelta(minutes=30)).isoformat());p.db.session.commit()
    p.db.session.add(p.base.WhatsAppDelivery(recipient_name='Test',recipient_phone='+4512345678',status='held'));p.db.session.commit()
    monitor.monitor_once();monitor.monitor_once()
    assert monitor.ConnectionChange.query.count()==3 and monitor.StorageReading.query.count()==1
    assert monitor.StorageReading.query.one().sms_used==9
    assert p.db.session.get(monitor.OperatingIncident,'sms_storage').bad_since
    assert p.db.session.get(monitor.OperatingIncident,'queue') is None
    gateway['modem']['state']='offline';monitor.monitor_once()
    assert monitor.ConnectionChange.query.count()==4 and p.db.session.get(monitor.ConnectionState,'modem').state=='usb:offline'


@pytest.mark.parametrize('raw,expected',[('+CPMS: "SM",3,30,"SM",3,30',(3,30)),('+CPMS: 0,50,0,50',(0,50)),('ERROR',(None,None)),('+CPMS: "SM",50,30',(None,None)),(None,(None,None))])
def test_storage_parses_optional_read_store(raw,expected):
    assert monitor.sms_capacity(raw)==expected


def configure_alerts(p,monkeypatch):
    monkeypatch.setenv('SMS_WHATSAPP_ALERT_CHANNEL','webhook');monkeypatch.setenv('SMS_WHATSAPP_ALERT_WEBHOOK_URL','https://admin.example/notify')
    p.operations.set_setting('alerts_enabled','true');p.db.session.commit()


def test_notices_delay_repeat_recovery_and_disabled_no_delivery(p,monkeypatch):
    configure_alerts(p,monkeypatch);calls=[];monkeypatch.setattr(monitor,'send_notice',lambda key,recovered:calls.append((key,recovered)))
    now=p.base.utcnow();monitor.update_incident('modem',True,now);p.db.session.commit()
    monitor.deliver_notices(now+timedelta(seconds=119));assert calls==[]
    monitor.deliver_notices(now+timedelta(seconds=120));monitor.deliver_notices(now+timedelta(minutes=5));assert calls==[('modem',False)]
    monitor.deliver_notices(now+timedelta(minutes=62));assert len(calls)==2
    monitor.update_incident('modem',False,now+timedelta(minutes=63));p.db.session.commit();monitor.deliver_notices(now+timedelta(minutes=63));monitor.deliver_notices(now+timedelta(minutes=70))
    assert calls[-1]==('modem',True) and len(calls)==3
    p.operations.set_setting('alerts_enabled','false');monitor.update_incident('modem',True,now);p.db.session.commit();monitor.deliver_notices(now+timedelta(hours=2));assert len(calls)==3


def test_notice_failure_is_sanitized_rate_limited_and_does_not_claim_recovery(p,monkeypatch):
    configure_alerts(p,monkeypatch);calls=[]
    def fail(*args):calls.append(args);raise RuntimeError('https://secret.example?token=SUPERSECRET')
    monkeypatch.setattr(monitor,'send_notice',fail)
    now=p.base.utcnow();monitor.update_incident('internet',True,now-timedelta(minutes=5));p.db.session.commit()
    monitor.deliver_notices(now);monitor.deliver_notices(now+timedelta(seconds=60));assert len(calls)==1
    row=p.db.session.get(monitor.OperatingIncident,'internet');assert 'SUPERSECRET' not in row.error and not row.notified_at
    monitor.update_incident('internet',False,now+timedelta(seconds=90));p.db.session.commit();monitor.deliver_notices(now+timedelta(minutes=10));assert len(calls)==1


def test_webhook_payload_has_no_alarm_details_and_redirects_disabled(p,monkeypatch):
    configure_alerts(p,monkeypatch);captured={}
    class Response:
        status=204
        def __enter__(self):return self
        def __exit__(self,*_):pass
    class Opener:
        def open(self,req,timeout):captured.update(payload=json.loads(req.data),timeout=timeout);return Response()
    monkeypatch.setattr(monitor.urllib.request,'build_opener',lambda handler:Opener())
    monitor.send_notice('modem',False)
    assert set(captured['payload'])=={'title','message','component','recovered'} and captured['timeout']==8
    assert monitor.NoRedirect().redirect_request(None,None,None,None,None,None) is None
    monkeypatch.setenv('SMS_WHATSAPP_ALERT_WEBHOOK_URL','http://unsafe.example');assert not monitor.alert_configuration()['ready']


def test_monitor_failure_does_not_change_delivery_jobs(authorized,monkeypatch):
    p=authorized;p.db.session.add(p.base.WhatsAppDelivery(recipient_name='Test',recipient_phone='+4512345678',status='pending'));p.db.session.commit()
    monkeypatch.setattr(monitor,'storage_reading',lambda *_:(_ for _ in ()).throw(OSError('full')))
    monitor.monitor_once()
    assert p.base.WhatsAppDelivery.query.one().status=='pending' and p.db.session.get(monitor.OperatingIncident,'disk').bad_since


def offsite_setup(p,monkeypatch,tmp_path):
    root=tmp_path/'remote';root.mkdir();(root/'.sbr-pager-offsite').write_text(offsite.MARKER)
    key=tmp_path/'key';key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv('SMS_WHATSAPP_OFFSITE_DIR',str(root));monkeypatch.setenv('SMS_WHATSAPP_OFFSITE_KEY_FILE',str(key))
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path/'local'))
    p.operations.set_setting('offsite_enabled','true');p.db.session.commit()
    name=p.operations.backup_database()
    return root,key,name


def test_encrypted_offsite_roundtrip_no_live_mutation_duplicate_copy_or_key_in_archive(authorized,tmp_path,monkeypatch):
    p=authorized;root,key,name=offsite_setup(p,monkeypatch,tmp_path)
    p.db.session.add(p.base.WhatsAppDelivery(recipient_name='Test',recipient_phone='+4512345678',status='pending'));p.db.session.commit()
    offsite.transfer_once();offsite.transfer_once()
    record=offsite.OffsiteTransfer.query.one();assert record.state=='passed'
    target=next(root.glob('*.fernet'));assert target.stat().st_mode&0o077==0
    assert b'Modtager' not in target.read_bytes() and p.base.WhatsAppDelivery.query.one().status=='pending'
    output=encrypted.decrypt_to_directory(target,key,tmp_path/'restore-check')
    assert set(p.name for p in output.iterdir())==set(encrypted.FILES)
    assert key.read_bytes() not in (output/'pager.json').read_bytes()
    with sqlite3.connect(output/'pager.sqlite') as c:assert c.execute('select name from recipient').fetchone()[0]=='Modtager'
    assert offsite.status_snapshot()['state']=='passed'
    with pytest.raises(FileExistsError):encrypted.decrypt_to_directory(target,key,output)


def test_offsite_missing_mount_does_not_create_folder_or_plaintext_or_retry_every_poll(p,tmp_path,monkeypatch):
    root,key,name=offsite_setup(p,monkeypatch,tmp_path);(root/'.sbr-pager-offsite').unlink()
    offsite.transfer_once();offsite.transfer_once()
    assert offsite.OffsiteTransfer.query.one().state=='failed' and not list(root.iterdir())
    assert (p.operations.backup_directory()/(name+'.sqlite')).exists()


def test_offsite_wrong_key_corruption_and_key_rotation(p,tmp_path,monkeypatch):
    root,key,name=offsite_setup(p,monkeypatch,tmp_path);offsite.transfer_once();target=next(root.glob('*.fernet'))
    other=tmp_path/'wrong-key';other.write_bytes(Fernet.generate_key())
    with pytest.raises(InvalidToken):encrypted.decrypt_to_directory(target,other,tmp_path/'bad')
    key.write_bytes(Fernet.generate_key());offsite.transfer_once()
    assert len(list(root.glob('*.fernet')))==2 and offsite.OffsiteTransfer.query.count()==2
    target.write_bytes(b'corrupt')
    with pytest.raises(InvalidToken):encrypted.decrypt_to_directory(target,key,tmp_path/'bad')


def test_offsite_size_limit_and_bad_archive_leave_output_absent(p,tmp_path,monkeypatch):
    root,key,name=offsite_setup(p,monkeypatch,tmp_path);monkeypatch.setattr(encrypted,'MAX_BYTES',1)
    offsite.transfer_once();assert offsite.OffsiteTransfer.query.one().state=='failed' and not list(root.glob('*.fernet'))
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:archive.writestr('../escape',b'bad')
    with pytest.raises(ValueError):encrypted.unpack(stream.getvalue())


def test_controls_disabled_defaults_invalid_config_and_csrf(p,client):
    assert not monitor.enabled() and offsite.status_snapshot()['state']=='disabled'
    assert client.post('/driftsvaern',data={'csrf_token':'test-csrf','alerts_enabled':'1'}).status_code==302
    assert not monitor.enabled()
    assert client.post('/driftsvaern/ekstern-backup',data={'csrf_token':'test-csrf','enabled':'1'}).status_code==302
    assert not offsite.enabled()
    assert client.post('/administratorer').status_code==400
    assert client.post('/driftsvaern').status_code==400


def test_queue_pause_and_offsite_disable_suppress_existing_notices(p,monkeypatch):
    configure_alerts(p,monkeypatch);calls=[];monkeypatch.setattr(monitor,'send_notice',lambda *args:calls.append(args))
    now=p.base.utcnow()
    for key in ['queue','offsite']:monitor.update_incident(key,True,now-timedelta(minutes=10))
    p.operations.set_setting('operation_mode','maintenance');p.operations.set_setting('operation_until',(now+timedelta(minutes=30)).isoformat());p.db.session.commit()
    monitor.deliver_notices(now)
    assert calls==[]
    monkeypatch.setenv('SMS_WHATSAPP_ALERT_WEBHOOK_URL','https://[')
    assert not monitor.alert_configuration()['ready']


def test_optional_modem_storage_queries_never_take_down_reader(g,monkeypatch):
    import modem_reader as usb
    import cudy_reader as cudy
    monkeypatch.setattr(usb,'command',lambda *args,**kwargs:'ERROR')
    assert usb.read_storage(None) is None
    class Client:
        def command(self,*args):raise RuntimeError('unsupported firmware')
    assert cudy.read_storage(Client()) is None


def test_smtp_uses_verified_tls_and_private_status_only(p,monkeypatch):
    import ssl
    for key,value in {'CHANNEL':'smtp','SMTP_HOST':'mail.example','SMTP_MODE':'starttls','SMTP_USERNAME':'operator','SMTP_PASSWORD':'SECRET','FROM':'pager@example.dk','TO':'admin@example.dk'}.items():monkeypatch.setenv('SMS_WHATSAPP_ALERT_'+key,value)
    calls=[]
    class SMTP:
        def __init__(self,host,port,timeout):calls.append((host,port,timeout))
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def ehlo(self):pass
        def starttls(self,context):assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED;calls.append('tls')
        def login(self,*args):calls.append('login')
        def send_message(self,message):assert 'SECRET' not in message.as_string() and '[SBR-SYSTEM]' in message.get_content();calls.append('sent')
    monkeypatch.setattr(monitor.smtplib,'SMTP',SMTP)
    assert monitor.alert_configuration()['ready']
    monitor.send_notice('whatsapp',False)
    assert calls==[('mail.example',587,8),'tls','login','sent']


def test_personal_cookie_is_not_accepted_as_old_shared_admin_cookie(p):
    client=personal_client(p,add_operator(p,role='viewer'))
    assert p.app.config['SESSION_COOKIE_NAME']!='session'
    assert client.get_cookie('sbr_pager_operator_session') is not None
    assert client.get_cookie('session') is None
