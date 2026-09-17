FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app

EXPOSE 8098

CMD ["gunicorn", "--bind", "0.0.0.0:8098", "--workers", "2", "--threads", "4", "--timeout", "30", "app:app"]
