FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py
COPY app.py base_app.py
COPY queued_app.py app.py
COPY modem_reader.py modem_reader_sbr.py gsm0338.py sms_pdu.py privacy_filter.py entrypoint.sh ./
RUN python -m py_compile app.py base_app.py modem_reader.py modem_reader_sbr.py gsm0338.py sms_pdu.py privacy_filter.py
RUN sh -n entrypoint.sh
RUN python -c 'from privacy_filter import redact_personal_data; s="(S) Brovej 1, 4180 Sorø\nPatient: Peter Jensen\nCPR: 010190-1234\nKontakt: +45 22 27 03 96\nMail: test@example.dk"; r=redact_personal_data(s); assert "Peter Jensen" not in r and "010190" not in r and "22 27 03 96" not in r and "test@example.dk" not in r and "Brovej 1, 4180 Sorø" in r'
RUN python -c 'from gsm0338 import decode_modem_bytes, encode_gsm0338; text="p\u00e5 f\u00e6rdigt h\u00f8stet \u00c6\u00d8\u00c5"; assert decode_modem_bytes(encode_gsm0338(text)) == text'
RUN python -c 'from sms_pdu import decode_sms_deliver_pdu; p="06915404969912040A91542272306900006280107085538031A8600A442DCFE920F80364EEC8C9E9331D8466CCE9653AE8DD963FC86550BB4C0675400CD003C4012D400E"; m=decode_sms_deliver_pdu(4,p); assert m.sender == "+4522270396"; assert m.text == "(A) Test p\u00e5 f\u00e6rdigt h\u00f8stet omr\u00e5de med \u00e6 \u00f8 \u00e5 \u00c6 \u00d8 \u00c5"'
RUN python -c 'from sms_pdu import DecodedSmsPart, assemble_parts; parts=[DecodedSmsPart(1,"+4511111111","Sending 2","2026-08-02T18:21:02Z"),DecodedSmsPart(9,"+4511111111","(A) Sending 1","2026-08-02T18:21:01Z")]; messages=assemble_parts(parts); assert [m["body"] for m in messages] == ["(A) Sending 1","Sending 2"]'
RUN python -c 'from sms_pdu import DecodedSmsPart, assemble_parts; parts=[DecodedSmsPart(4,"+4550202739","(S)M+V · Bygn.brand-Villa/Rækkehus · ild i ","2026-09-11T17:50:00Z",42,2,1),DecodedSmsPart(7,"+4550202739","større træ klods op af hus","2026-09-11T17:50:01Z",42,2,2)]; m=assemble_parts(parts); assert len(m)==1 and "større træ klods op af hus" in m[0]["body"] and m[0]["indices"]==[4,7]'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-build.db python -c 'from app import detect_station_code; assert detect_station_code("(ISL) Test") == "ISL"; assert detect_station_code("ISL Test") == "ISL"; assert detect_station_code("(A) Test") == "A"; assert detect_station_code("(B) Test") == "B"; assert detect_station_code("mislykket") is None'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-station-build.db python -c 'from datetime import datetime, timezone; from base_app import build_vagtbytte_payload; p=build_vagtbytte_payload("+4512345678","Brand",datetime(2026,8,2,20,0,tzinfo=timezone.utc),"sms-1","A"); assert p["stationCode"] == "A"; assert p["rawMessage"] == "Brand"'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-vagtbytte-disabled-build.db GATEWAY_STATUS_FILE=/tmp/sms-gateway-vagtbytte-disabled-status.json VAGTBYTTE_FORWARD_ENABLED=false SMS_GATEWAY_API_TOKEN=build-test-token python -c 'import json; from pathlib import Path; from app import app; c=app.test_client(); h={"Authorization":"Bearer build-test-token"}; r=c.post("/api/incoming", json={"sender":"+4512345678","body":"(A) TEST"}, headers=h); j=r.get_json(); assert r.status_code == 201 and j["vagtbytte_disabled"] is True and j["vagtbytte_created"] is False; s=json.loads(Path("/tmp/sms-gateway-vagtbytte-disabled-status.json").read_text()); assert not s.get("last_vagtbytte_success_at")'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-outbox-build.db SMS_GATEWAY_API_TOKEN=build-test-token python -c 'from app import app; c=app.test_client(); h={"Authorization":"Bearer build-test-token"}; assert c.post("/api/outgoing", json={}, headers={}).status_code == 401; q=c.post("/api/outgoing", json={"recipient":"+4512345678","body":"test"}, headers=h); assert q.status_code == 202; i=q.get_json()["id"]; claim=c.post("/api/outgoing/claim", json={}, headers=h); assert claim.status_code == 200 and claim.get_json()["id"] == i; done=c.post(f"/api/outgoing/{i}/complete", json={"status":"sent"}, headers=h); status=c.get(f"/api/outgoing/{i}", headers=h).get_json(); assert status["status"] == "sent"'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-command-build.db RACHER_MONITOR_SMS_TO=+4512345678 SMS_GATEWAY_API_TOKEN=build-test-token python -c 'from app import app; c=app.test_client(); h={"Authorization":"Bearer build-test-token"}; r=c.post("/api/incoming", json={"sender":"+4512345678","body":"status"}, headers=h); assert r.status_code == 201 and r.get_json()["vagtbytte_created"] is False; q=c.post("/api/commands/claim", json={}, headers=h); assert q.status_code == 200 and q.get_json()["sender"] == "+4512345678" and q.get_json()["command"] == "status"; i=q.get_json()["id"]; done=c.post(f"/api/commands/{i}/complete", json={"status":"done"}, headers=h); assert done.status_code == 200 and done.get_json()["status"] == "done"'
RUN SBR_PAGER_PARENT_STATE_FILE=/tmp/sbr-parent-test.json python -c 'import modem_reader_sbr as m; m.remember_parent_event("+4512345678","event-primary","S"); assert m.recent_parent_event_key("+4512345678")=="event-primary"; assert m.recent_parent_event_key("+4587654321") is None'
RUN SBR_PAGER_PARENT_STATE_FILE=/tmp/sbr-auto-followup-test.json SBR_PAGER_FOLLOWUP_WINDOW_SECONDS=60 python -c 'import modem_reader_sbr as m; sender="+4512345678"; m.remember_parent_event(sender,"event-s","S"); follow={"sender":sender,"body":"Ekstra oplysninger fra vagtcentralen","timestamp":"2026-09-19T12:00:20Z","indices":[1]}; meta=m.complete_message_metadata(follow); assert meta[2]=="followup_complete" and meta[5]=="event-s"; fresh={"sender":sender,"body":"(A)M+V · Ny alarm","timestamp":"2026-09-19T12:00:30Z","indices":[2]}; meta=m.complete_message_metadata(fresh); assert meta[2]=="alarm_complete" and meta[5] is None'
RUN chmod +x /app/entrypoint.sh && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080
CMD ["/app/entrypoint.sh"]
