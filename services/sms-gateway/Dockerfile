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
COPY modem_reader.py gsm0338.py sms_pdu.py ./
RUN python -m py_compile app.py base_app.py modem_reader.py gsm0338.py sms_pdu.py
RUN python -c 'from gsm0338 import decode_modem_bytes, encode_gsm0338; text="p\u00e5 f\u00e6rdigt h\u00f8stet \u00c6\u00d8\u00c5"; assert decode_modem_bytes(encode_gsm0338(text)) == text'
RUN python -c 'from sms_pdu import decode_sms_deliver_pdu; p="06915404969912040A91542272306900006280107085538031A8600A442DCFE920F80364EEC8C9E9331D8466CCE9653AE8DD963FC86550BB4C0675400CD003C4012D400E"; m=decode_sms_deliver_pdu(4,p); assert m.sender == "+4522270396"; assert m.text == "(A) Test p\u00e5 f\u00e6rdigt h\u00f8stet omr\u00e5de med \u00e6 \u00f8 \u00e5 \u00c6 \u00d8 \u00c5"'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-build.db python -c 'from app import detect_station_code; assert detect_station_code("(ISL) Test") == "ISL"; assert detect_station_code("ISL Test") == "ISL"; assert detect_station_code("(A) Test") == "A"; assert detect_station_code("mislykket") is None'
RUN DATABASE_URL=sqlite:////tmp/sms-gateway-outbox-build.db python -c 'from app import app; c=app.test_client(); q=c.post("/api/outgoing", json={"recipient":"+4512345678","body":"test"}); assert q.status_code == 202; i=q.get_json()["id"]; claim=c.post("/api/outgoing/claim", json={}); assert claim.status_code == 200 and claim.get_json()["id"] == i; done=c.post(f"/api/outgoing/{i}/complete", json={"status":"sent"}); assert done.status_code == 200; status=c.get(f"/api/outgoing/{i}").get_json(); assert status["status"] == "sent"'
RUN mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080
CMD ["sh", "-c", "python modem_reader.py & reader_pid=$!; trap 'kill $reader_pid' TERM INT; gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 --timeout 60 app:app & web_pid=$!; wait $web_pid; kill $reader_pid 2>/dev/null || true"]
