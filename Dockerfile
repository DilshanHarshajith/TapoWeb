# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

# Keep Python output unbuffered and skip bytecode files so the app runs cleanly
# as a non-root user on a read-only filesystem if desired.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TAPO_WEB_HOST=0.0.0.0 \
    TAPO_WEB_PORT=8080

# ffmpeg is required for the live HLS stream and snapshots. tini reaps the
# ffmpeg child process and forwards signals so `docker stop` shuts down fast.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so this layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py camera.py stream.py tapo_api.py recordings.py ./
COPY static ./static

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && chown -R app:app /app

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ.get('TAPO_WEB_PORT', '8080'), timeout=4)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "app.py"]
