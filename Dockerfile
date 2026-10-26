FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SOURCER_ENV=production \
    SOURCER_DATA_DIR=/data

WORKDIR /app
COPY requirements-cloud.txt .
RUN pip install -r requirements-cloud.txt

COPY . .
RUN mkdir -p /data

# One worker on purpose: the background job (importing, scoring, the AI agent) and its live log
# live in this process. Threads give the web pages their concurrency.
CMD ["sh", "-c", "gunicorn 'sourcer.web.app:create_app()' --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 300 --access-logfile -"]
