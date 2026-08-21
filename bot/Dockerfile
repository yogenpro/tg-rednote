FROM python:3.12-slim

LABEL org.opencontainers.image.title="tg-rednote" \
      org.opencontainers.image.description="Telegram bot that turns Xiaohongshu (RedNote) share links into native Telegram media" \
      org.opencontainers.image.source="https://github.com/yogenpro/tg-rednote" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Defaults that make `docker run` work without a compose file. Every one of
    # them is overridable with -e.
    STATE_PATH=/data/state.json \
    HEARTBEAT_PATH=/tmp/heartbeat \
    XHS_DOWNLOADER_URL=http://xhs-downloader:5556

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The state file holds the XHS cookie, so it lives on a volume owned by an
# unprivileged user; the process itself is disposable (PLAN §5).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /data && chown bot:bot /data
USER bot
VOLUME /data

# Long polling has no port to probe, so liveness is "the poller came back
# round recently". A wedged or crash-looping bot stops touching the heartbeat
# and the container goes unhealthy instead of sitting there looking fine.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import os,sys,time; p=os.environ['HEARTBEAT_PATH']; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 180 else 1)"]

CMD ["python", "-m", "app.main"]
