# Deployra (panel) — budować z katalogu głównego repo:
#   docker build -f frontend/Dockerfile -t deployra-panel .
FROM python:3.13-alpine

# wireguard-tools/wireguard-go: panel jest peerem tunelu (gdy nie stoi obok bramki)
RUN apk add --no-cache bash iproute2 wireguard-tools wireguard-go

WORKDIR /app

COPY shared /opt/deployra/shared
COPY frontend/requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build build-base libffi-dev \
    && pip install --no-cache-dir /opt/deployra/shared -r /tmp/requirements.txt \
    && apk del .build

COPY frontend/deployra_panel ./deployra_panel
RUN python -m compileall -q /app/deployra_panel

LABEL deployra.component=panel

ENV PYTHONUNBUFFERED=1 \
    DEPLOYRA_DATA=/var/lib/deployra-panel \
    DEPLOYRA_BIND=0.0.0.0 \
    DEPLOYRA_PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD wget -qO /dev/null http://127.0.0.1:${DEPLOYRA_PORT}/healthz || exit 1
CMD ["python", "-m", "deployra_panel"]
