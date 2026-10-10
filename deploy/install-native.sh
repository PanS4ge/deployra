#!/usr/bin/env bash
# Deployra Backend jako usługa systemd — bez Dockera na hoście (np. Proxmox VE).
# Backend wykryje działające kontenery LXC z Dockerem w środku i będzie nimi zarządzał.
#
#   sudo bash deploy/install-native.sh                  # tryb bezpośredni (kod parowania w journalctl)
#   sudo DEPLOYRA_GATEWAY=vps:51821 DEPLOYRA_JOIN=dplr1.… bash deploy/install-native.sh   # przez tunel
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/deployra-backend
DOCKER_CLI_VERSION="${DOCKER_CLI_VERSION:-27.3.1}"
ARCH="$(uname -m)"   # x86_64 / aarch64

[ "$(id -u)" -eq 0 ] || { echo "Uruchom jako root"; exit 1; }

echo "» Pakiety systemowe"
apt-get update -qq
apt-get install -y -qq python3 python3-venv wireguard-tools iptables iproute2 git openssh-client curl ca-certificates >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  echo "» Klient Dockera (tylko CLI — do stacków compose i buildów w kontenerach LXC)"
  tmp="$(mktemp -d)"
  curl -fsSL "https://download.docker.com/linux/static/stable/${ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" | tar xz -C "$tmp" docker/docker
  install -m 755 "$tmp/docker/docker" /usr/local/bin/docker
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  rm -rf "$tmp"
fi

echo "» Środowisko Pythona w $PREFIX"
python3 -m venv "$PREFIX"
"$PREFIX/bin/pip" install -q --upgrade pip
"$PREFIX/bin/pip" install -q "$REPO_DIR/shared" -r "$REPO_DIR/backend/requirements.txt"
rm -rf "$PREFIX/deployra_backend"
cp -r "$REPO_DIR/backend/deployra_backend" "$PREFIX/"

ENV_FILE=/etc/deployra-backend.env
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
DEPLOYRA_NAME=$(hostname)
DEPLOYRA_PROXMOX=1
DEPLOYRA_DATA=/var/lib/deployra
DEPLOYRA_SRV=/srv/deployra
DEPLOYRA_GATEWAY=${DEPLOYRA_GATEWAY:-}
DEPLOYRA_JOIN=${DEPLOYRA_JOIN:-}
EOF
  chmod 600 "$ENV_FILE"
fi

cat > /etc/systemd/system/deployra-backend.service <<EOF
[Unit]
Description=Deployra Backend (węzeł)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
WorkingDirectory=$PREFIX
ExecStart=$PREFIX/bin/python -m deployra_backend
Restart=on-failure
RestartSec=5
MemoryMax=200M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now deployra-backend
echo "✔ Gotowe. Kod parowania / stan dołączania: journalctl -u deployra-backend -f"
