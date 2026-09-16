FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py events_app.py stats_v2.py station_filter_app.py poller.py ./
RUN python -m py_compile app.py events_app.py stats_v2.py station_filter_app.py poller.py
RUN DATABASE_URL=sqlite:////tmp/sms-whatsapp-events-build.db SMS_WHATSAPP_INGEST_TOKEN=build-token python -c 'from events_app import app,db,base,AlarmEvent; ctx=app.app_context(); ctx.push(); db.create_all(); db.session.add(base.AllowedSender(name="test",phone="+4512345678",active=True)); db.session.commit(); c=app.test_client(); h={"Authorization":"Bearer build-token"}; p={"sender":"+4512345678","body":"🚨 ALARM MODTAGET\n(S)M+V · Bygn.brand-Villa/Rækkehus","rawBody":"(S)M+V · Bygn.brand-Villa/Rækkehus","receivedAt":"2026-09-13T07:00:00Z","sourceMessageId":"pre-1","eventKey":"event-1","groupKey":"event-1","messageKind":"alarm_prealert","partCurrent":1,"partTotal":2}; r=c.post("/api/incoming",json=p,headers=h); assert r.status_code==201; p.update(body="🚨 KOMPLET ALARM\n(S)M+V · Bygn.brand-Villa/Rækkehus · Testvej 12, 4200 Slagelse",rawBody="(S)M+V · Bygn.brand-Villa/Rækkehus · Testvej 12, 4200 Slagelse",sourceMessageId="complete-1",messageKind="alarm_complete",partCurrent=2); r=c.post("/api/incoming",json=p,headers=h); assert r.status_code==201; e=AlarmEvent.query.one(); assert e.status=="complete" and e.station=="S" and e.alarm_type=="Bygn.brand-Villa/Rækkehus"; ctx.pop()'
RUN DATABASE_URL=sqlite:////tmp/sms-whatsapp-stats-build.db python -c 'import stats_v2; assert stats_v2.app is not None'
RUN DATABASE_URL=sqlite:////tmp/sms-whatsapp-station-filter-build.db python -c 'import station_filter_app as s; ctx=s.app.app_context(); ctx.push(); s.db.create_all(); r=s.base.Recipient(name="R",phone="+4512345678",active=True); s.db.session.add(r); s.db.session.commit(); assert s.recipient_accepts(r.id,"S"); s.db.session.add(s.RecipientStationFilter(recipient_id=r.id,station="S")); s.db.session.commit(); assert s.recipient_accepts(r.id,"S") and not s.recipient_accepts(r.id,"A"); assert s.detect_station("🚨 ALARM\n(S)M+V") == "S"; ctx.pop()'
RUN mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "60", "station_filter_app:app"]
