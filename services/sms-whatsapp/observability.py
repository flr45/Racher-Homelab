"""Measured operations, recipient overview and isolated recovery checks."""
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo

from flask import abort,flash,redirect,render_template,request,session,url_for
from sqlalchemy import case
import operations as ops
import delivery_retry_app as delivery
import station_filter_app as stations
import diagnostics
import restore_db

app,base,db=ops.app,ops.base,ops.db
log=logging.getLogger('pager-observability')
_probe_check_at=0.0

class DeliveryAttempt(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    delivery_id=db.Column(db.Integer,db.ForeignKey('whats_app_delivery.id',ondelete='CASCADE'),nullable=False,index=True)
    started_at=db.Column(db.DateTime,nullable=False,index=True)
    success=db.Column(db.Boolean,nullable=False)
    processing_ms=db.Column(db.Integer)
    queue_ms=db.Column(db.Integer)
    network_ms=db.Column(db.Integer,nullable=False)
    total_ms=db.Column(db.Integer)

class BackupVerification(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    backup_name=db.Column(db.String(64),nullable=False,index=True)
    checked_at=db.Column(db.DateTime,nullable=False,default=base.utcnow,index=True)
    status=db.Column(db.String(20),nullable=False)
    elapsed_ms=db.Column(db.Integer,nullable=False)
    detail=db.Column(db.String(255),nullable=False)
    table_counts=db.Column(db.Text)

class CommissioningStep(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    driver=db.Column(db.String(10),nullable=False)
    step=db.Column(db.String(40),nullable=False)
    result=db.Column(db.String(20),nullable=False)
    notes=db.Column(db.String(1000))
    actor=db.Column(db.String(120),nullable=False)
    updated_at=db.Column(db.DateTime,nullable=False,default=base.utcnow)
    __table_args__=(db.UniqueConstraint('driver','step'),)


def milliseconds(start,end):
    if start is None or end is None:
        return None
    difference=(delivery._aware(end)-delivery._aware(start)).total_seconds()*1000
    return round(difference) if difference>=0 else None


def record_attempt(row,inbound,started_at,elapsed,success):
    decision=delivery.InboundDecision.query.filter_by(inbound_id=inbound.id).first()
    processed=decision.created_at if decision else None
    db.session.add(DeliveryAttempt(delivery_id=row.id,started_at=started_at,success=success,
        processing_ms=milliseconds(inbound.created_at,processed),queue_ms=milliseconds(processed or inbound.created_at,started_at),
        network_ms=max(0,round(elapsed*1000)),total_ms=milliseconds(inbound.received_at,base.utcnow())))
    db.session.commit()


def calendar_window(value=None):
    try:
        date=datetime.strptime(value,'%Y-%m-%d').date() if value else base.utcnow().astimezone(ZoneInfo('Europe/Copenhagen')).date()
    except ValueError:
        abort(400,'Ugyldig dato')
    start=datetime.combine(date,datetime.min.time(),ZoneInfo('Europe/Copenhagen'))
    end=start+timedelta(days=1)
    return date.isoformat(),start.astimezone(timezone.utc),end.astimezone(timezone.utc)


def daily_summary(value=None):
    date,start,end=calendar_window(value)
    inbound=base.InboundMessage.query.filter(base.InboundMessage.created_at>=start,base.InboundMessage.created_at<end)
    sent=base.WhatsAppDelivery.query.filter(base.WhatsAppDelivery.status=='sent',base.WhatsAppDelivery.attempted_at>=start,base.WhatsAppDelivery.attempted_at<end)
    measured=DeliveryAttempt.query.filter(DeliveryAttempt.started_at>=start,DeliveryAttempt.started_at<end)
    stats=db.session.query(db.func.count(DeliveryAttempt.id),db.func.sum(case((DeliveryAttempt.success.is_(False),1),else_=0)),
        db.func.avg(DeliveryAttempt.processing_ms),db.func.avg(DeliveryAttempt.queue_ms),db.func.avg(DeliveryAttempt.network_ms),
        db.func.avg(DeliveryAttempt.total_ms),db.func.max(DeliveryAttempt.total_ms)).filter(DeliveryAttempt.started_at>=start,DeliveryAttempt.started_at<end).one()
    phases=[{'label':label,'ms':round(value) if value is not None else None} for label,value in zip(['Behandling i Pager','Ventetid før forsøget','OpenWA-svartid'],stats[2:5])]
    known=[p for p in phases if p['ms'] is not None]
    pilot=inbound.join(ops.RoutingReview,ops.RoutingReview.inbound_id==base.InboundMessage.id).count()
    backup=BackupVerification.query.order_by(BackupVerification.id.desc()).first()
    return {'date':date,'received':inbound.count(),'rejected':inbound.filter_by(accepted=False).count(),'pilot':pilot,
        'sent':sent.count(),'attempts':stats[0],'failed_attempts':int(stats[1] or 0),'phases':phases,
        'total_ms':round(stats[5]) if stats[5] is not None else None,'max_total_ms':round(stats[6]) if stats[6] is not None else None,
        'bottleneck':max(known,key=lambda p:p['ms'])['label'] if known else None,'backup':backup,'ops':ops.operation_snapshot()}


@app.get('/driftsrapport')
@base.login_required
def daily_report():
    return render_template('daily_report.html',title='Daglig driftsoversigt',report=daily_summary(request.args.get('date')))

@app.get('/modtageroversigt')
@base.login_required
def recipient_matrix():
    selections=stations.subscription_map()
    rows=base.Recipient.query.order_by(base.Recipient.name).all()
    return render_template('recipient_matrix.html',title='Modtagere pr. station',rows=rows,codes=stations.STATIONS,
        selections={r.id:selections.get(r.id,{'*'}) for r in rows},accepts=stations.accepts_selection)

@app.post('/modtageroversigt')
@base.login_required
@ops.audited
def bulk_subscriptions():
    base.check_csrf()
    try:
        ids={int(value) for value in request.form.getlist('recipient_ids')}
    except ValueError:
        abort(400,'Ugyldigt modtagervalg')
    selected=set(request.form.getlist('stations'))
    if not 1<=len(ids)<=100 or not selected or not selected<=set(stations.STATIONS)|{'*'}:
        abort(400,'Vælg 1–100 modtagere og mindst ét stationsvalg')
    if request.form.get('confirm')!='1':
        abort(400,'Bekræft at stationsvalgene erstattes')
    rows=base.Recipient.query.filter(base.Recipient.id.in_(ids)).all()
    if len(rows)!=len(ids):
        abort(409,'En valgt modtager er fjernet; genindlæs oversigten')
    if '*' in selected:
        selected={'*'}|({'TEST'} if 'TEST' in selected else set())
    stations.RecipientStationFilter.query.filter(stations.RecipientStationFilter.recipient_id.in_(ids)).delete(synchronize_session=False)
    for row in rows:
        for code in sorted(selected):db.session.add(stations.RecipientStationFilter(recipient_id=row.id,station=code))
    db.session.commit()
    flash(f'Stationsvalg erstattet for {len(rows)} modtagere. Aktiv-status og numre er bevaret.')
    return redirect(url_for('recipient_matrix'))

@app.get('/modtagerfejl')
@base.login_required
def recipient_failures():
    cutoff=base.utcnow()-timedelta(days=7)
    aggregate=db.session.query(base.WhatsAppDelivery.recipient_phone,
        db.func.count(DeliveryAttempt.id),db.func.sum(case((DeliveryAttempt.success.is_(False),1),else_=0)),
        db.func.max(DeliveryAttempt.started_at)).join(DeliveryAttempt,DeliveryAttempt.delivery_id==base.WhatsAppDelivery.id).filter(DeliveryAttempt.started_at>=cutoff).group_by(base.WhatsAppDelivery.recipient_phone).all()
    current={r.phone:r for r in base.Recipient.query.all()}
    unresolved=db.session.query(base.WhatsAppDelivery.recipient_phone,db.func.count(base.WhatsAppDelivery.id)).filter(base.WhatsAppDelivery.status.in_(('failed','retrying','held'))).group_by(base.WhatsAppDelivery.recipient_phone).all()
    data={phone:{'phone':phone,'recipient':current.get(phone),'attempts':count,'failures':int(failures or 0),'last':last,'unresolved':0} for phone,count,failures,last in aggregate}
    for phone,count in unresolved:
        row=data.setdefault(phone,{'phone':phone,'recipient':current.get(phone),'attempts':0,'failures':0,'last':None})
        row['unresolved']=count
    rows=sorted(data.values(),key=lambda r:(r['unresolved'],r['failures']),reverse=True)
    phone=request.args.get('phone','')
    logs=base.WhatsAppDelivery.query.filter_by(recipient_phone=phone).order_by(base.WhatsAppDelivery.id.desc()).limit(100).all() if phone else []
    return render_template('recipient_failures.html',title='Modtagerspecifik fejlhistorik',rows=rows,phone=phone,logs=logs)

@app.get('/delte-sms')
@base.login_required
def multipart_page():
    import dashboard_app
    try:
        data=dashboard_app.gateway_request('/api/multipart')
        if not isinstance(data,dict) or not isinstance(data.get('groups'),list):raise ValueError('invalid response')
    except Exception:
        data={'state':'unknown','groups':[],'checked_at':None}
    for row in data['groups']:
        for key in ('first_seen','last_seen','completed_at'):
            try:row[key]=base.parse_received_at(row[key]) if row.get(key) else None
            except ValueError:row[key]=None
    return render_template('multipart.html',title='Kontrol af delte SMS’er',data=data)


@delivery.serialized
def verify_backup(name):
    started=time.monotonic()
    status,detail,counts='passed','Prøvegendannelse og integritetskontrol bestået',{}
    try:
        source=ops.backup_path(name,'.sqlite')
        config_path=ops.backup_path(name,'.json')
        ops.validated_config(json.loads(config_path.read_text()))
        with tempfile.TemporaryDirectory(prefix='pager-recovery-check-') as directory:
            target=Path(directory)/'isolated.sqlite'
            with sqlite3.connect(Path(db.engine.url.database).resolve().as_uri()+'?mode=ro',uri=True) as live,sqlite3.connect(target) as copy:
                deadline=time.monotonic()+30
                def progress(*_):
                    if time.monotonic()>deadline:raise TimeoutError('snapshot timeout')
                live.backup(copy,pages=256,progress=progress,sleep=.05)
            restore_db.restore(source,target)
            with sqlite3.connect(target) as test:
                if test.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or test.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('integrity')
                if test.execute("SELECT count(*) FROM whats_app_delivery WHERE status IN ('pending','retrying','held','sending')").fetchone()[0]:raise ValueError('active jobs')
                for table in ('recipient','allowed_sender','inbound_message','whats_app_delivery'):
                    counts[table]=test.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    except Exception:
        status,detail='failed','Prøvegendannelse fejlede. Kontrollér backupfil, ledig plads og om programversionens databaseskema passer.'
        log.exception('Isoleret backupkontrol fejlede')
    result=BackupVerification(backup_name=name,status=status,elapsed_ms=max(0,round((time.monotonic()-started)*1000)),detail=detail,table_counts=json.dumps(counts))
    db.session.add(result);db.session.commit()
    return result

@app.post('/drift/backup/<name>/kontrol')
@base.login_required
def backup_probe(name):
    base.check_csrf()
    # Validate before the checker so nonexistent paths get a real 404.
    ops.backup_path(name,'.sqlite')
    result=verify_backup(name)
    ops.record_audit('Prøvegendannelse af backup',{'backup':name,'status':result.status})
    flash(result.detail,'message' if result.status=='passed' else 'error')
    return redirect(url_for('backup_checks'))

@app.get('/backupkontrol')
@base.login_required
def backup_checks():
    return render_template('backup_checks.html',title='Kontrol af backups',backups=ops.backup_list(),checks=BackupVerification.query.order_by(BackupVerification.id.desc()).limit(30).all())


def scheduled_probe():
    global _probe_check_at
    now=time.monotonic()
    if _probe_check_at and now-_probe_check_at<60:return
    _probe_check_at=now
    today=base.utcnow().astimezone(ZoneInfo('Europe/Copenhagen')).date().isoformat()
    if ops.setting('last_daily_backup')!=today or ops.setting('last_backup_probe_day')==today:return
    latest=BackupVerification.query.filter_by(status='passed').order_by(BackupVerification.id.desc()).first()
    if latest and base.utcnow()-delivery._aware(latest.checked_at)<timedelta(days=7):return
    backups=ops.backup_list()
    if not backups:return
    verify_backup(backups[0]['name'])
    ops.set_setting('last_backup_probe_day',today);db.session.commit()

_original_housekeeping=ops.housekeeping
def extended_housekeeping():
    _original_housekeeping()
    try:scheduled_probe()
    except Exception:
        db.session.rollback();log.exception('Kunne ikke planlægge prøvegendannelse')


STEPS=[
 ('backup','Sikkerhedsbackup','Tag og download en backup. Kør prøvegendannelse og kontrollér resultatet.'),
 ('network','Internet og adgang','Kontrollér DHCP, gateway/DNS, internet og Tailscale. Brug Opstartskontrol; SMS-status alene beviser ikke dataforbindelse.'),
 ('modem','SIM og modem','Kontrollér aktiv kilde, SIM/PIN, signal og mobilnet under Forbindelser. Ved LT300 bruges den læsende Cudy-forbindelsestest.'),
 ('short','Kort SMS','Aktivér tidsbegrænset prøvetilstand og send en kort, tydeligt markeret test-SMS til modemmet. Kontrollér afsender og besked i historikken.'),
 ('multipart','Delt SMS','Send en lang test-SMS i prøvetilstand. Kontrollér alle delnumre, eventuelle manglende dele og sammenlægning under Delte SMS’er og beskedhistorik.'),
 ('routing','Stationsvalg','Afprøv relevante stationer samt Test-opt-in i Forhåndsvisning. Kontrollér de beregnede modtagere; prøver skal ikke nå rigtige alarmmodtagere.'),
 ('whatsapp','WhatsApp-kvittering','Vælg normal drift, send en test til ét valgt nummer og kontrollér både OpenWA-kvittering og modtagelse på telefonen.'),
 ('restart','Genstart','Brug vedligeholdelse, gem en test i køen og genstart Pager efter backup. Kontrollér kø og tilstand. Godkend kun tilbageholdte testjobs efter kontrol.'),
 ('outage','Internetudfald','Afbryd internet kortvarigt med lokal adgang og mulighed for at forbinde igen. Kontrollér offline-status, bevaret testkø og genopretning; ingen rigtige alarmer bruges til testen.'),
 ('usb','USB-backup','Ved LT300: afprøv det manuelle skift tilbage til USB med samme SIM. Kontrollér én testmodtagelse, og vælg derefter den ønskede primære kilde.'),
 ('smsout','Udgående SMS','USB-afsendelse kan afprøves særskilt. LT300-afsendelse er uverificeret og skal markeres Afventer, indtil firmware og fysisk afsendelse er undersøgt.'),
 ('release','Normal drift','Vælg normal drift og korrekt SMS-kilde, kontrollér modtagere, kø, backup og watchdogs. Gem eventuelle udeståender før ibrugtagning.')]

@app.get('/indkoering')
@base.login_required
def commissioning_page():
    driver=request.args.get('driver','cudy')
    if driver not in ('usb','cudy'):abort(400)
    results={row.step:row for row in CommissioningStep.query.filter_by(driver=driver).all()}
    return render_template('commissioning.html',title='Indkøringsforløb',driver=driver,steps=STEPS,results=results)

@app.post('/indkoering/<driver>/<step>')
@base.login_required
@delivery.serialized
def save_commissioning(driver,step):
    base.check_csrf()
    result=request.form.get('result')
    if driver not in ('usb','cudy') or step not in {s[0] for s in STEPS} or result not in {'pending','passed','failed','skipped'}:abort(400)
    if result=='passed' and request.form.get('confirm')!='1':abort(400,'Bekræft at testen er udført fysisk')
    if driver=='cudy' and step=='smsout' and result=='passed':abort(409,'LT300-afsendelse er endnu ikke understøttet eller verificeret')
    row=CommissioningStep.query.filter_by(driver=driver,step=step).first()
    if not row:row=CommissioningStep(driver=driver,step=step,result=result,actor=base.admin_username());db.session.add(row)
    row.result,row.notes,row.actor,row.updated_at=result,request.form.get('notes','').strip()[:1000],base.admin_username(),base.utcnow()
    db.session.commit();ops.record_audit('Indkøringstest',{'driver':driver,'step':step,'result':result})
    return redirect(url_for('commissioning_page',driver=driver)+'#'+step)


FILTER_KEYS={'q','sender','station','status','from','to'}
@app.before_request
def remember_history_filters():
    if request.endpoint!='message_history' or not base.logged_in():return
    if request.args.get('reset')=='1':session.pop('history_filters',None);return redirect(url_for('message_history'))
    supplied={k:request.args.get(k,'')[:200] for k in FILTER_KEYS if k in request.args}
    if supplied:session['history_filters']=supplied
    elif not request.args and session.get('history_filters'):
        return redirect(url_for('message_history',**session['history_filters']))


with app.app_context():db.create_all()
delivery.operations_measure=record_attempt
delivery.operations_housekeeping=extended_housekeeping
