import importlib.util
import json
from pathlib import Path
from datetime import timedelta
import pytest
import observability as o
from test_delivery import ingest

@pytest.mark.parametrize('path',['/driftsrapport','/modtageroversigt','/modtagerfejl','/delte-sms','/backupkontrol','/indkoering'])
def test_added_pages_authenticated_and_render(p,client,path,monkeypatch):
    monkeypatch.setattr(p,'gateway_request',lambda *a,**k:{'state':'current','groups':[]})
    assert p.app.test_client().get(path).status_code==302
    assert client.get(path).status_code==200


def test_bulk_changes_only_selected_station_subscriptions(authorized,client):
    p=authorized
    first=p.base.Recipient.query.one()
    other=p.base.Recipient(name='Other',phone='+4533333333',active=False);p.db.session.add(other);p.db.session.commit()
    response=client.post('/modtageroversigt',data={'csrf_token':'test-csrf','recipient_ids':[str(first.id)],'stations':['A','S','TEST'],'confirm':'1'})
    assert response.status_code==302
    assert p.stations.configured_stations(first.id)=={'A','S','TEST'}
    assert p.stations.configured_stations(other.id)=={'*'} and not other.active
    assert p.base.Recipient.query.count()==2 and o.ops.AuditEntry.query.count()==1

@pytest.mark.parametrize('data',[{'stations':['A'],'confirm':'1'},{'recipient_ids':['bad'],'stations':['A'],'confirm':'1'},{'recipient_ids':['1'],'stations':['bad'],'confirm':'1'},{'recipient_ids':['1'],'stations':['A']},{'recipient_ids':['1'],'confirm':'1'}])
def test_bulk_validation_is_atomic(authorized,client,data):
    response=client.post('/modtageroversigt',data={'csrf_token':'test-csrf',**data})
    assert response.status_code==400
    assert authorized.stations.RecipientStationFilter.query.count()==0


def test_measure_failure_then_success_and_recipient_history(authorized,client,monkeypatch):
    p=authorized
    ingest(p)
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:(_ for _ in ()).throw(RuntimeError('simulated offline')))
    p.deliveries.retry_due_once()
    attempt=o.DeliveryAttempt.query.one()
    assert not attempt.success and attempt.network_ms>=0 and attempt.processing_ms is not None
    state=p.deliveries.WhatsAppRetryState.query.one();state.next_attempt_at=p.base.utcnow();p.db.session.commit()
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:'receipt')
    p.deliveries.retry_due_once()
    assert o.DeliveryAttempt.query.count()==2 and o.DeliveryAttempt.query.filter_by(success=True).count()==1
    report=o.daily_summary()
    assert report['attempts']==2 and report['failed_attempts']==1 and report['bottleneck']
    response=client.get('/modtagerfejl?phone=%2B4522222222')
    assert b'Modtager' in response.data and b'receipt' in response.data


def test_metric_failure_does_not_replay_success(authorized,monkeypatch):
    p=authorized;ingest(p);calls=[]
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:calls.append(a) or 'receipt')
    monkeypatch.setattr(p.deliveries,'operations_measure',lambda *a:(_ for _ in ()).throw(RuntimeError('stats failure')))
    p.deliveries.retry_due_once();p.deliveries.retry_due_once()
    assert len(calls)==1 and p.base.WhatsAppDelivery.query.one().status=='sent'


def test_daily_window_handles_danish_dst_and_invalid_date(p,client):
    _,start,end=o.calendar_window('2026-10-25')
    assert (end-start).total_seconds()==25*3600
    _,start,end=o.calendar_window('2026-03-29')
    assert (end-start).total_seconds()==23*3600
    assert client.get('/driftsrapport?date=invalid').status_code==400
    assert o.milliseconds(p.base.utcnow(),p.base.utcnow()-timedelta(seconds=1)) is None


def test_backup_probe_restores_isolated_db_and_preserves_live_jobs(authorized,client,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    ingest(p);row=p.base.WhatsAppDelivery.query.one()
    name=o.ops.backup_database()
    result=o.verify_backup(name)
    assert result.status=='passed' and json.loads(result.table_counts)['inbound_message']==1
    p.db.session.refresh(row)
    assert row.status=='pending' and o.ops.runtime_mode()['name']=='normal'
    assert client.post(f'/drift/backup/{name}/kontrol',data={'csrf_token':'test-csrf'}).status_code==302
    assert client.get('/backupkontrol').status_code==200


def test_corrupted_backup_fails_probe_without_touching_live_db(authorized,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    name=o.ops.backup_database();(tmp_path/(name+'.sqlite')).write_bytes(b'bad SQLite')
    assert o.verify_backup(name).status=='failed'
    assert p.base.Recipient.query.one().name=='Modtager'


def test_scheduled_probe_weekly_after_daily_backup(authorized,tmp_path,monkeypatch):
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    o.ops.backup_database()
    today=o.calendar_window()[0];o.ops.set_setting('last_daily_backup',today);authorized.db.session.commit()
    monkeypatch.setattr(o,'_probe_check_at',0)
    o.scheduled_probe()
    assert o.BackupVerification.query.one().status=='passed'
    o._probe_check_at=0;o.scheduled_probe()
    assert o.BackupVerification.query.count()==1


def test_commissioning_records_manual_result_and_cannot_claim_cudy_sending(client,p):
    data={'csrf_token':'test-csrf','result':'passed','notes':'Checked'}
    assert client.post('/indkoering/cudy/short',data=data).status_code==400
    assert client.post('/indkoering/cudy/short',data={**data,'confirm':'1'}).status_code==302
    assert o.CommissioningStep.query.one().result=='passed'
    assert client.post('/indkoering/cudy/smsout',data={**data,'confirm':'1'}).status_code==409
    assert client.post('/indkoering/usb/smsout',data={**data,'confirm':'1'}).status_code==302
    assert client.get('/indkoering?driver=invalid').status_code==400


def test_history_filters_remember_reset_and_stay_per_session(client,p):
    assert client.get('/beskeder?q=needle&station=A').status_code==200
    response=client.get('/beskeder');assert response.status_code==302 and 'needle' in response.location
    other=p.app.test_client()
    with other.session_transaction() as s:s.update(admin_authenticated=True,auth_kind='environment',auth_fingerprint=p.operator_accounts.environment_fingerprint())
    assert other.get('/beskeder').status_code==200
    assert client.get('/beskeder?reset=1').status_code==302
    assert client.get('/beskeder').status_code==200

@pytest.mark.parametrize('path',['/modtageroversigt','/indkoering/usb/short'])
def test_added_mutations_need_csrf(client,path):
    assert client.post(path,data={'result':'passed','confirm':'1'}).status_code==400


def test_multipart_monitor_real_sequences_timestamp_variations_and_auth(g,p,monkeypatch,tmp_path):
    import multipart_monitor as monitor
    from sms_pdu import DecodedSmsPart
    monkeypatch.setenv('SMS_MULTIPART_STATUS_FILE',str(tmp_path/'monitor.json'))
    now=p.base.utcnow()
    one=DecodedSmsPart(1,'+4512345678','SECRET','2026-10-01T00:00:00Z',3,3,1)
    three=DecodedSmsPart(3,'+4512345678','SECRET','2026-10-01T00:00:01Z',3,3,3)
    monitor.observe([one,three],now)
    group=monitor.read_status()['groups'][0]
    assert group['seen']==[1,3] and group['missing']==[2] and group['state']=='missing'
    two=DecodedSmsPart(2,'+4512345678','SECRET','2026-10-01T00:00:02Z',3,3,2)
    monitor.observe([one,two,three],now+timedelta(seconds=8))
    group=monitor.read_status()['groups'][0]
    assert group['state']=='complete' and group['wait_seconds']==8 and len(monitor.read_status()['groups'])==1
    raw=(tmp_path/'monitor.json').read_text()
    assert 'SECRET' not in raw and '+4512345678' not in raw
    assert g.app.test_client().get('/api/multipart').status_code==401
    assert g.app.test_client().get('/api/multipart',headers={'Authorization':'Bearer test-gateway'}).json['groups'][0]['missing']==[]


def test_multipart_late_first_part_does_not_duplicate_completed_group(p,monkeypatch,tmp_path):
    import multipart_monitor as monitor
    from sms_pdu import DecodedSmsPart
    monkeypatch.setenv('SMS_MULTIPART_STATUS_FILE',str(tmp_path/'monitor.json'))
    now=p.base.utcnow()
    first=DecodedSmsPart(1,'+4512345678','body','2026-10-01T00:00:00Z',5,2,1)
    second=DecodedSmsPart(2,'+4512345678','body','2026-10-01T00:00:01Z',5,2,2)
    monitor.observe([second],now)
    monitor.observe([first,second],now+timedelta(seconds=5))
    monitor.observe([first,second],now+timedelta(seconds=8))
    rows=monitor.read_status()['groups']
    assert len(rows)==1 and rows[0]['state']=='complete' and rows[0]['wait_seconds']==5


def test_multipart_disappearance_corruption_and_observer_failure_dont_raise(p,monkeypatch,tmp_path):
    import multipart_monitor as monitor
    from sms_pdu import DecodedSmsPart
    target=tmp_path/'parts.json';monkeypatch.setenv('SMS_MULTIPART_STATUS_FILE',str(target))
    now=p.base.utcnow();part=DecodedSmsPart(1,'+4512345678','body','2026-10-01T00:00:00Z',4,2,1)
    monitor.observe([part],now);monitor.observe([],now+timedelta(seconds=3))
    assert monitor.read_status()['groups'][0]['state']=='disappeared'
    target.write_text('bad json');assert monitor.read_status()['state']=='unknown'
    monkeypatch.setattr(monitor,'path',lambda:(_ for _ in ()).throw(OSError('disk error')))
    monitor.observe([part])
