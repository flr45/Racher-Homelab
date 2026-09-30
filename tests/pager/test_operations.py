import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
import operations as ops
from test_delivery import ingest


def mode(p, name, minutes=30):
    ops.set_setting('operation_mode', name)
    ops.set_setting('operation_until', (p.base.utcnow()+timedelta(minutes=minutes)).isoformat())
    p.db.session.commit()

@pytest.mark.parametrize('path',['/drift','/aendringer','/forhaandsvisning','/beskeder','/opstartskontrol','/driftsvisning','/fejlrapport.json'])
def test_new_views_require_login(p,path):
    assert p.app.test_client().get(path).status_code == 302

@pytest.mark.parametrize('path',['/drift','/aendringer','/forhaandsvisning','/beskeder','/opstartskontrol','/driftsvisning','/fejlrapport.json'])
def test_new_views_render(client,path):
    assert client.get(path).status_code == 200


def test_preview_has_no_delivery_side_effects(authorized,client):
    p=authorized
    response=client.post('/forhaandsvisning',data={'csrf_token':'test-csrf','sender':'+4512345678','body':'(A)M+V · Brand'})
    assert response.status_code==200 and 'Modtager'.encode() in response.data
    assert p.base.InboundMessage.query.count()==0 and p.base.WhatsAppDelivery.query.count()==0
    response=client.post('/forhaandsvisning',data={'csrf_token':'test-csrf','sender':'+4599999999','body':'(A)M+V · Brand'})
    assert b'godkendt' in response.data


def test_pilot_records_targets_and_never_replays(authorized,monkeypatch):
    p=authorized
    calls=[]
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:calls.append(a) or 'receipt')
    mode(p,'pilot')
    assert ingest(p).status_code==201
    assert ops.RoutingReview.query.count()==1
    assert json.loads(ops.RoutingReview.query.one().targets)[0]['phone']=='+4522222222'
    assert p.base.WhatsAppDelivery.query.count()==0
    mode(p,'normal')
    assert ingest(p).json['duplicate']
    p.deliveries.retry_due_once()
    assert not calls and p.base.WhatsAppDelivery.query.count()==0


def test_maintenance_queues_then_resumes(authorized,monkeypatch):
    p=authorized
    calls=[]
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:calls.append(a) or 'receipt')
    mode(p,'maintenance')
    ingest(p)
    assert p.base.WhatsAppDelivery.query.one().status=='pending'
    assert p.deliveries.retry_due_once()['sent']==0 and not calls
    ops.set_setting('operation_until',(p.base.utcnow()-timedelta(seconds=1)).isoformat());p.db.session.commit()
    assert p.deliveries.retry_due_once()['sent']==1 and len(calls)==1

@pytest.mark.parametrize('name',['maintenance','pilot'])
def test_manual_test_paths_cannot_bypass_pause(authorized,client,monkeypatch,name):
    p=authorized
    r=p.base.Recipient.query.one()
    test=p.diagnostics.enqueue_single_test(r,'single-paused')
    calls=[]
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:calls.append(a) or 'receipt')
    mode(p,name)
    assert client.post('/test',data={'csrf_token':'test-csrf'}).status_code==409
    assert client.post('/enkelt-test',data={'csrf_token':'test-csrf','test_token':'anything','recipient_id':r.id}).status_code==409
    p.diagnostics.run_single_test()
    assert not calls and test.status=='pending'


def test_synchronous_compatibility_still_respects_pause(authorized,monkeypatch):
    p=authorized
    mode(p,'maintenance')
    monkeypatch.setattr(p.deliveries,'ASYNC_DELIVERY',False)
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:pytest.fail('sent during maintenance'))
    ingest(p)
    assert p.base.WhatsAppDelivery.query.one().status=='pending'


def test_old_alarm_held_and_retry_all_does_not_release(authorized,client,monkeypatch):
    p=authorized
    calls=[]
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:calls.append(a) or 'receipt')
    ingest(p)
    state=p.deliveries.WhatsAppRetryState.query.one()
    state.first_failed_at=p.base.utcnow()-timedelta(minutes=20);p.db.session.commit()
    p.deliveries.retry_due_once()
    row=p.base.WhatsAppDelivery.query.one()
    assert row.status=='held' and not calls
    assert client.post('/leveringsko/genforsog',data={'csrf_token':'test-csrf'}).status_code==302
    p.deliveries.retry_due_once()
    assert row.status=='held' and state.next_attempt_at is None and not calls
    assert client.post(f'/drift/ko/{row.inbound_id}',data={'csrf_token':'test-csrf','action':'release','confirm':'1'}).status_code==302
    p.deliveries.retry_due_once()
    assert row.status=='sent' and len(calls)==1
    client.post(f'/drift/ko/{row.inbound_id}',data={'csrf_token':'test-csrf','action':'release','confirm':'1'})
    p.deliveries.retry_due_once()
    assert len(calls)==1


def test_cancel_held_and_pause_release_guard(authorized,client):
    p=authorized
    ingest(p);row=p.base.WhatsAppDelivery.query.one();row.status='held';p.db.session.commit()
    mode(p,'maintenance')
    assert client.post(f'/drift/ko/{row.inbound_id}',data={'csrf_token':'test-csrf','action':'release','confirm':'1'}).status_code==409
    assert client.post(f'/drift/ko/{row.inbound_id}',data={'csrf_token':'test-csrf','action':'cancel','confirm':'1'}).status_code==302
    assert row.status=='cancelled'


def test_duplicate_warning_does_not_block_legitimate_alarm(authorized):
    p=authorized
    ingest(p,'one');ingest(p,'two')
    assert p.base.WhatsAppDelivery.query.count()==2
    assert ops.DuplicateHint.query.one().inbound_id==p.base.InboundMessage.query.filter_by(source_id='two').one().id


def test_source_id_collision_is_rejected(authorized):
    p=authorized
    ingest(p)
    assert ingest(p,body='different real alarm').status_code==409
    assert p.base.WhatsAppDelivery.query.count()==1

@pytest.mark.parametrize('body',['✅ SBR Pager · forbindelsestest #2\nKun sendt til det valgte nummer.','[SBR-SYSTEM] drift','✅ TEST · Testbesked fra SBR Pager'])
def test_own_system_fingerprints_are_not_forwarded(authorized,body):
    p=authorized
    ingest(p,body=body)
    assert p.base.WhatsAppDelivery.query.count()==0
    assert p.deliveries.InboundDecision.query.one().decision=='loop_blocked'


def test_generic_status_text_is_not_blocked(authorized):
    p=authorized
    ingest(p,body='(A) Status: yderligere mandskab ønskes')
    assert p.base.WhatsAppDelivery.query.count()==1


def test_test_sms_marked_and_multi_station_selection_retained(authorized,monkeypatch):
    p=authorized
    r=p.base.Recipient.query.one()
    p.db.session.add_all([p.stations.RecipientStationFilter(recipient_id=r.id,station=s) for s in ['A','S','TEST']]);p.db.session.commit()
    calls=[]
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:calls.append(a) or 'receipt')
    ingest(p,'test',body='TEST alarm (S)')
    p.deliveries.retry_due_once()
    assert calls[0][1].startswith('🧪 TEST · ØVELSE')
    assert p.stations.recipient_accepts(r.id,'A') and p.stations.recipient_accepts(r.id,'S') and not p.stations.recipient_accepts(r.id,'L')


def test_audit_records_actor_and_changes_without_secrets(authorized,client):
    p=authorized
    r=p.base.Recipient.query.one()
    assert client.post(f'/brugere/{r.id}/rediger',data={'csrf_token':'test-csrf','name':'Nyt navn','phone':r.phone,'active':'1'}).status_code==302
    entry=ops.AuditEntry.query.one()
    assert entry.actor=='admin' and 'Nyt navn' in entry.changes and 'Modtager' in entry.changes
    assert 'test-adgangskode' not in entry.changes
    before=ops.AuditEntry.query.count()
    client.post(f'/brugere/{r.id}/rediger',data={'csrf_token':'invalid','name':'bad','phone':r.phone})
    assert ops.AuditEntry.query.count()==before


def test_backup_is_consistent_and_download_requires_auth(authorized,client,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    ingest(p)
    name=ops.backup_database()
    with sqlite3.connect(tmp_path/(name+'.sqlite')) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('SELECT count(*) FROM inbound_message').fetchone()[0]==1
    manifest=json.loads((tmp_path/(name+'.json')).read_text())
    assert ops.validated_config(manifest)['recipients'][0]['phone']=='+4522222222'
    assert client.get(f'/drift/backup/{name}/database').status_code==200
    assert p.app.test_client().get(f'/drift/backup/{name}/database').status_code==302
    assert client.get('/drift/backup/not-a-backup/config').status_code==404


def test_restore_configuration_safety_backup_and_no_replays(authorized,client,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    name=ops.backup_database()
    r=p.base.Recipient.query.one();r.name='changed';p.db.session.commit()
    ingest(p)
    p.diagnostics.enqueue_single_test(r,'restore-pending')
    assert client.post(f'/drift/backup/{name}/gendan',data={'csrf_token':'test-csrf','confirm':'GENDAN'}).status_code==302
    assert p.base.Recipient.query.one().name=='Modtager'
    assert p.base.InboundMessage.query.count()==1
    assert {r.status for r in p.base.WhatsAppDelivery.query.all()}=={'cancelled'}
    assert p.diagnostics.SingleWhatsAppTest.query.one().recipient_id is None
    assert ops.runtime_mode()['name']=='maintenance'
    assert len(list(tmp_path.glob('*.sqlite')))==2
    mode(p,'normal')
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:pytest.fail('replayed after restore'))
    p.deliveries.retry_due_once();p.diagnostics.run_single_test()


def test_restore_rejects_corrupt_configuration_without_changes(authorized,client,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path));name=ops.backup_database()
    path=tmp_path/(name+'.json');manifest=json.loads(path.read_text());manifest['config']['filters']=[{'id':1,'recipient_id':999,'station':'A'}];path.write_text(json.dumps(manifest))
    response=client.post(f'/drift/backup/{name}/gendan',data={'csrf_token':'test-csrf','confirm':'GENDAN'})
    assert response.status_code==302 and p.base.Recipient.query.one().name=='Modtager'
    assert ops.runtime_mode()['name']=='normal'


def test_full_database_restore_preserves_history_but_cancels_old_jobs(authorized,tmp_path):
    import restore_db
    p=authorized
    ingest(p)
    source=Path(p.db.engine.url.database)
    backup=tmp_path/'backup.sqlite';target=tmp_path/'restored.sqlite'
    with sqlite3.connect(source) as conn,sqlite3.connect(backup) as dest:conn.backup(dest)
    with sqlite3.connect(source) as conn,sqlite3.connect(target) as dest:conn.backup(dest)
    safe=restore_db.restore(backup,target)
    assert safe.exists()
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT count(*) FROM inbound_message').fetchone()[0]==1
        assert conn.execute('SELECT status FROM whats_app_delivery').fetchone()[0]=='cancelled'
        assert conn.execute("SELECT value FROM pager_runtime_setting WHERE key='operation_mode'").fetchone()[0]=='maintenance'
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_full_restore_rejects_incompatible_schema(authorized,tmp_path):
    import restore_db
    p=authorized
    source=tmp_path/'bad.sqlite'
    with sqlite3.connect(source) as conn:conn.execute('CREATE TABLE unrelated(x)')
    with pytest.raises(ValueError):restore_db.restore(source,Path(p.db.engine.url.database))
    assert p.base.Recipient.query.count()==1


def test_daily_backup_once_per_day(authorized,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    monkeypatch.setattr(ops,'_housekeeping_at',0)
    monkeypatch.setattr(ops,'_backup_retry_at',0)
    ops.housekeeping();assert len(list(tmp_path.glob('*.sqlite')))==1
    ops._housekeeping_at=0;ops.housekeeping()
    assert len(list(tmp_path.glob('*.sqlite')))==1


def test_retention_preserves_active_events_and_jobs(authorized,monkeypatch):
    p=authorized
    ingest(p,'old-complete');ingest(p,'old-active',body='(A)M+V · Brand')
    cutoff=p.base.utcnow()-timedelta(days=100)
    for event in p.events.AlarmEvent.query.all():event.last_update_at=cutoff
    for row in p.base.InboundMessage.query.all():row.created_at=cutoff
    first=p.base.InboundMessage.query.filter_by(source_id='old-complete').one()
    row=p.base.WhatsAppDelivery.query.filter_by(inbound_id=first.id).one();row.status='sent';p.db.session.commit()
    assert ops.prune_history(30)==1
    assert p.base.InboundMessage.query.one().source_id=='old-active'
    assert p.base.WhatsAppDelivery.query.one().status=='pending'
    assert p.events.AlarmEvent.query.count()==1


def test_reports_and_display_do_not_expose_message_or_credentials(authorized,client):
    p=authorized
    ingest(p,body='(A) Fortrolig adresse og tekst')
    for path in ['/fejlrapport.json','/driftsvisning']:
        response=client.get(path)
        assert response.status_code==200
        for value in ['Fortrolig adresse','+4512345678','+4522222222','test-ingest','test-adgangskode']:
            assert value.encode() not in response.data


def test_history_search_details_and_date_filters(authorized,client):
    p=authorized
    ingest(p,'find',body='(A) unik søgetekst');ingest(p,'other',body='(S) anden tekst')
    result=client.get('/beskeder?q=unik&station=A&from=2026-09-30&to=2026-09-30')
    assert result.status_code==200 and 'unik søgetekst'.encode() in result.data and b'anden tekst' not in result.data
    row=p.base.InboundMessage.query.filter_by(source_id='find').one()
    result=client.get(f'/beskeder/{row.id}')
    assert result.status_code==200 and b'Modtager' in result.data
    assert client.get('/beskeder?from=invalid').status_code==400


def test_activity_traffic_and_missing_station_checks(authorized):
    p=authorized
    for i in range(5):ingest(p,str(i))
    ops.set_setting('traffic_threshold',5);p.db.session.commit()
    snapshot=ops.operation_snapshot()
    assert snapshot['traffic_warning'] and snapshot['last_sms'] is not None
    assert ops.config_issues()==['Ingen aktive modtagere for Test']

@pytest.mark.parametrize('path',['/drift/indstillinger','/drift/backup','/forhaandsvisning','/drift/ko/1'])
def test_mutating_operations_require_csrf(client,path):
    assert client.post(path,data={'mode':'normal'}).status_code==400


def test_interrupted_alarm_is_held_on_restart_without_resending(authorized,monkeypatch):
    p=authorized
    ingest(p)
    row=p.base.WhatsAppDelivery.query.one();row.status='sending';p.db.session.commit()
    assert ops.recover_interrupted_alarms()==1
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:pytest.fail('resent uncertain alarm'))
    p.deliveries.retry_due_once()
    assert row.status=='held' and 'uafklaret' in row.error
    assert ops.recover_interrupted_alarms()==0


def test_failed_old_job_retry_button_still_requires_age_review(authorized,client,monkeypatch):
    p=authorized
    ingest(p);row=p.base.WhatsAppDelivery.query.one();row.status='failed'
    state=p.deliveries.WhatsAppRetryState.query.one()
    state.first_failed_at=p.base.utcnow()-timedelta(minutes=30);state.completed_at=p.base.utcnow();state.next_attempt_at=None;p.db.session.commit()
    client.post('/leveringsko/genforsog',data={'csrf_token':'test-csrf'})
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:pytest.fail('age bypassed'))
    p.deliveries.retry_due_once()
    assert row.status=='held'


def test_non_ascii_phone_digits_are_rejected(p):
    with pytest.raises(ValueError):p.base.normalize_phone('+451234٥٦٧٨')


def test_backup_failure_blocks_pruning_and_is_visible(authorized,monkeypatch):
    p=authorized
    ops.set_setting('retention_days',30);p.db.session.commit()
    monkeypatch.setattr(ops,'_housekeeping_at',0);monkeypatch.setattr(ops,'_backup_retry_at',0)
    monkeypatch.setattr(ops,'backup_database',lambda:(_ for _ in ()).throw(OSError('disk full')))
    monkeypatch.setattr(ops,'prune_history',lambda *a:pytest.fail('pruned without backup'))
    ops.housekeeping()
    assert 'fejlede' in ops.operation_snapshot()['housekeeping_error']
    ops._housekeeping_at=0;ops.housekeeping()
    assert ops.setting('last_daily_backup')==''
    assert 'fejlede' in ops.operation_snapshot()['housekeeping_error']


def test_backup_retention_keeps_fourteen_complete_pairs(authorized,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    for i in range(16):ops.backup_database()
    assert len(list(tmp_path.glob('*.sqlite')))==14 and len(list(tmp_path.glob('*.json')))==14


def test_retention_cleans_terminal_tests_but_keeps_pending(authorized):
    p=authorized
    recipient=p.base.Recipient.query.one()
    old=p.diagnostics.enqueue_single_test(recipient,'old-test');old.status='sent';old.created_at=p.base.utcnow()-timedelta(days=50)
    row=p.db.session.get(p.base.WhatsAppDelivery,old.delivery_id);row.status='sent';row.attempted_at=old.created_at
    pending=p.diagnostics.enqueue_single_test(recipient,'active-test');p.db.session.commit()
    ops.prune_history(30)
    assert p.diagnostics.SingleWhatsAppTest.query.one().id==pending.id
    assert p.base.WhatsAppDelivery.query.one().status=='pending'


def test_terminal_failure_can_be_explicitly_closed_without_losing_history(authorized,client,monkeypatch):
    p=authorized
    ingest(p);row=p.base.WhatsAppDelivery.query.one();row.status='failed';p.db.session.commit()
    assert client.post(f'/drift/ko/{row.inbound_id}',data={'csrf_token':'test-csrf','confirm':'1','action':'cancel'}).status_code==302
    assert row.status=='cancelled' and p.base.InboundMessage.query.count()==1
    assert p.deliveries.queue_snapshot()['failed']==0
    monkeypatch.setattr(p.base,'send_whatsapp',lambda *a:pytest.fail('closed job sent'))
    p.deliveries.retry_due_once()


def test_pilot_followup_keeps_simulated_parent_station_and_real_stats_unchanged(authorized):
    p=authorized
    mode(p,'pilot')
    ingest(p,'pilot-main',body='(A)M+V · Brand',eventKey='pilot-key',groupKey='pilot-key',messageKind='alarm_complete')
    ingest(p,'pilot-follow',body='Ekstra oplysninger',parentEventKey='pilot-key',messageKind='followup_complete')
    review=ops.RoutingReview.query.order_by(ops.RoutingReview.id.desc()).first()
    assert review.station=='A' and json.loads(review.targets)[0]['name']=='Modtager'
    assert p.events.AlarmEvent.query.count()==0 and p.events.stats_snapshot()['today']==0
    assert p.base.WhatsAppDelivery.query.count()==0


def test_existing_usb_status_reply_fingerprint_cannot_loop(authorized):
    p=authorized
    ingest(p,body='PI OK - temp 40C - disk 35% - RAM 21% - load 0.5 - Docker 3/3 - up 2d5t')
    assert p.deliveries.InboundDecision.query.one().decision=='loop_blocked'
    assert p.base.WhatsAppDelivery.query.count()==0


def test_daily_backup_runs_on_first_cycle_with_short_machine_uptime(authorized,tmp_path,monkeypatch):
    p=authorized
    monkeypatch.setenv('SMS_WHATSAPP_BACKUP_DIR',str(tmp_path))
    monkeypatch.setattr(ops,'_housekeeping_at',0)
    monkeypatch.setattr(ops,'_backup_retry_at',0)
    monkeypatch.setattr(ops.time,'monotonic',lambda:10.0)
    ops.housekeeping()
    assert len(list(tmp_path.glob('*.sqlite')))==1
    assert ops.setting('last_daily_backup')
