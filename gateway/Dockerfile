# Deployra-WG — budować z katalogu głównego repo:
#   docker build -f gateway/Dockerfile -t deployra-wg .
# nginx działa obok, w oficjalnym obrazie nginx:stable-alpine (zob. deploy/gateway.compose.yml).
FROM python:3.13-alpine

ARG TARGETARCH
ARG CLOUDFLARED_VERSION=latest

RUN apk add --no-cache bash iproute2 iptables ip6tables wireguard-tools wireguard-go lego ca-certificates wget

# cloudflared (opcjonalne wejście ruchu przez Cloudflare Tunnel) — statyczna binarka z wydań Cloudflare
RUN arch="${TARGETARCH:-amd64}"; \
    if [ "$CLOUDFLARED_VERSION" = "latest" ]; then base="https://github.com/cloudflare/cloudflared/releases/latest/download"; \
    else base="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}"; fi; \
    wget -q -O /usr/local/bin/cloudflared "${base}/cloudflared-linux-${arch}" \
    && chmod +x /usr/local/bin/cloudflared \
    && cloudflared --version

WORKDIR /app

COPY shared /opt/deployra/shared
COPY gateway/requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build build-base libffi-dev \
    && pip install --no-cache-dir /opt/deployra/shared -r /tmp/requirements.txt \
    && apk del .build

COPY gateway/deployra_gateway ./deployra_gateway
RUN python -m compileall -q /app/deployra_gateway

ENV PYTHONUNBUFFERED=1 \
    DEPLOYRA_DATA=/var/lib/deployra-wg

EXPOSE 51820/udp 51821
CMD ["python", "-m", "deployra_gateway"]
