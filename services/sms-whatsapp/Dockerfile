FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py poller.py ./
RUN python -m py_compile app.py poller.py && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 8080
CMD ["sh", "-c", "python poller.py & poller_pid=$!; trap 'kill $poller_pid' TERM INT; gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 --timeout 60 app:app & web_pid=$!; wait $web_pid; kill $poller_pid 2>/dev/null || true"]
