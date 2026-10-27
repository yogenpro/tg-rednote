FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/yogenpro/tg-rednote"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/home
WORKDIR /srv
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin login \
    && mkdir -p /run/xhs-login && chown login:login /run/xhs-login \
    && chmod 0700 /run/xhs-login
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py .
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json,socket; s=socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect('/run/xhs-login/login.sock'); s.sendall(b'{\"action\":\"health\"}\\n'); assert json.loads(s.recv(1024)) == {'status':'ready'}"]
CMD ["python", "worker.py"]
