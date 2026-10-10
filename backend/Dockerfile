# Deployra Backend — budować z katalogu głównego repo:
#   docker build -f backend/Dockerfile -t deployra-backend .
FROM python:3.13-alpine

RUN apk add --no-cache \
        bash iproute2 iptables ip6tables \
        wireguard-tools wireguard-go \
        docker-cli docker-cli-compose docker-cli-buildx \
        git openssh-client

WORKDIR /app

COPY shared /opt/deployra/shared
COPY backend/requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build build-base linux-headers libffi-dev \
    && pip install --no-cache-dir /opt/deployra/shared -r /tmp/requirements.txt \
    && apk del .build

COPY backend/deployra_backend ./deployra_backend

ENV PYTHONUNBUFFERED=1 \
    DEPLOYRA_DATA=/var/lib/deployra \
    DEPLOYRA_SRV=/srv/deployra

EXPOSE 2750 2222
CMD ["python", "-m", "deployra_backend"]
