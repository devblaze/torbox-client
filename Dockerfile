FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# gosu for privilege drop; passwd provides usermod/groupmod for PUID/PGID remap.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 app \
    && useradd -u 1000 -g app -d /app -s /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Defaults; override via environment / compose / Unraid template.
ENV DOWNLOAD_DIR=/downloads \
    DATA_DIR=/data \
    LISTEN_PORT=8080 \
    PUID=1000 \
    PGID=1000 \
    UMASK=022

VOLUME ["/downloads", "/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
