# Deployra-WG — budować z katalogu głównego repo:
#   docker build -f gateway/Dockerfile -t deployra-wg .
# nginx działa obok, w oficjalnym obrazie nginx:stable-alpine (zob. deploy/gateway.compose.yml).
FROM python:3.13-alpine

RUN apk add --no-cache bash iproute2 iptables ip6tables wireguard-tools wireguard-go lego ca-certificates

WORKDIR /app

COPY shared /opt/deployra/shared
COPY gateway/requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build build-base libffi-dev \
    && pip install --no-cache-dir /opt/deployra/shared -r /tmp/requirements.txt \
    && apk del .build

COPY gateway/deployra_gateway ./deployra_gateway

ENV PYTHONUNBUFFERED=1 \
    DEPLOYRA_DATA=/var/lib/deployra-wg

EXPOSE 51820/udp 51821
CMD ["python", "-m", "deployra_gateway"]
