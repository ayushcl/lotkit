FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY requirements-runtime.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir \
        -r requirements-runtime.txt


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN groupadd --gid 10001 lotkit \
    && useradd --uid 10001 --gid lotkit --create-home \
        --home-dir /home/lotkit --shell /usr/sbin/nologin lotkit \
    && install -d --owner=lotkit --group=lotkit --mode=0700 /var/data

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=lotkit:lotkit api ./api
COPY --chown=lotkit:lotkit cfr_buyers_guides_english.pdf .

USER lotkit:lotkit

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.parse, urllib.request; port = os.environ.get('PORT', '8000'); host = urllib.parse.urlsplit(os.environ.get('LOTKIT_PUBLIC_BASE_URL', '')).netloc or '127.0.0.1'; request = urllib.request.Request('http://127.0.0.1:' + port + '/health', headers={'Host': host}); urllib.request.urlopen(request, timeout=3).read()"

CMD ["sh", "-c", "exec python -m uvicorn api.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1 --proxy-headers --forwarded-allow-ips=\"${LOTKIT_FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
