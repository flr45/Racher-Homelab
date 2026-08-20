FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAGER_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py app_core.py gateway.py pdl_multiline.py storage.py routing.py adaptive.py adaptive_routes.py ric_noise_filter.py training.py training_routes.py push_service.py operations.py rss_updates.py alarm_rules.py pushover_destinations.py wsgi.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data

EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/healthz', timeout=2)"

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8088", "--access-logfile", "-", "wsgi:app"]
