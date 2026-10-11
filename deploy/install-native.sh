#!/usr/bin/env bash
# Deployra Backend jako usługa systemd — bez Dockera na hoście (np. Proxmox VE).
# Backend wykryje działające kontenery LXC z Dockerem w środku i będzie nimi zarządzał.
#
#   sudo bash deploy/install-native.sh                  # tryb bezpośredni (kod parowania w journalctl)
#   sudo DEPLOYRA_GATEWAY=vps:51821 DEPLOYRA_JOIN=dplr1.… bash deploy/install-native.sh   # przez tunel
#   sudo bash deploy/install-native.sh --update 1.2.0   # aktualizacja (zwykle zleca ją panel przez systemd-run)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/deployra-backend
ENV_FILE=/etc/deployra-backend.env
DOCKER_CLI_VERSION="${DOCKER_CLI_VERSION:-27.3.1}"
ARCH="$(uname -m)"   # x86_64 / aarch64

[ "$(id -u)" -eq 0 ] || { echo "Uruchom jako root"; exit 1; }

# --- aktualizacja: git do nowego taga, potem dalsza część NOWEJ wersji tego skryptu ---------------
UPDATE_VERSION=""
if [ "${1:-}" = "--update" ]; then
  UPDATE_VERSION="${2:?podaj wersję, np. --update 1.2.0}"
  UPDATE_VERSION="${UPDATE_VERSION#v}"
  DATA_DIR="$(sed -n 's/^DEPLOYRA_DATA=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)"
  STATUS_DIR="${DATA_DIR:-/var/lib/deployra}/update"
  mkdir -p "$STATUS_DIR"
  status() {
    printf 'state=%s\nversion=%s\nmessage=%s\nupdated=%s\n' "$1" "$UPDATE_VERSION" "$2" "$(date +%s)" >"$STATUS_DIR/.status.tmp"
    mv -f "$STATUS_DIR/.status.tmp" "$STATUS_DIR/status"
  }
  fail() { echo "✖ $*"; status error "$*"; exit 1; }
  trap 'status error "aktualizacja przerwana (linia $LINENO) — szczegóły w $STATUS_DIR/log"' ERR

  if [ -z "${DEPLOYRA_UPDATE_STAGE:-}" ]; then
    : >"$STATUS_DIR/log"
    exec >>"$STATUS_DIR/log" 2>&1
    echo "$(date '+%F %T') Aktualizacja do $UPDATE_VERSION (instalacja natywna, repo $REPO_DIR)"
    git_() { git -c safe.directory='*' -C "$REPO_DIR" "$@"; }
    status running "pobieram kod (git fetch)"
    git_ fetch --tags --force --quiet origin || fail "git fetch nie powiódł się — brak sieci albo dostępu do repozytorium"
    ref="v$UPDATE_VERSION"
    git_ rev-parse -q --verify "refs/tags/$ref^{commit}" >/dev/null || ref="$UPDATE_VERSION"
    git_ rev-parse -q --verify "refs/tags/$ref^{commit}" >/dev/null || fail "w repozytorium nie ma taga v$UPDATE_VERSION"
    git_ diff --quiet HEAD -- || fail "lokalne zmiany w $REPO_DIR — zatwierdź je albo zaktualizuj ręcznie"
    branch="$(git_ symbolic-ref -q --short HEAD || true)"
    if [ -n "$branch" ]; then
      git_ merge --ff-only --quiet "$ref" || fail "gałęzi $branch nie da się przewinąć do $ref"
    else
      git_ -c advice.detachedHead=false checkout --quiet "$ref" || fail "nie udało się przełączyć na $ref"
    fi
    echo "Kod: $(git_ log -1 --format='%h %s')"
    DEPLOYRA_UPDATE_STAGE=install exec bash "$REPO_DIR/deploy/install-native.sh" --update "$UPDATE_VERSION"
  fi
  status running "instaluję nową wersję"
fi

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
"$PREFIX/bin/pip" install -q --force-reinstall --no-deps "$REPO_DIR/shared"   # ta sama wersja też ma się podmienić
"$PREFIX/bin/pip" install -q "$REPO_DIR/shared" -r "$REPO_DIR/backend/requirements.txt"
rm -rf "$PREFIX/deployra_backend"
cp -r "$REPO_DIR/backend/deployra_backend" "$PREFIX/"

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
# skąd brać kod przy aktualizacjach z panelu
grep -q '^DEPLOYRA_NATIVE_REPO=' "$ENV_FILE" || echo "DEPLOYRA_NATIVE_REPO=$REPO_DIR" >> "$ENV_FILE"

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
if [ -n "$UPDATE_VERSION" ]; then
  systemctl restart deployra-backend
  status done "zaktualizowano do $UPDATE_VERSION"
  echo "✔ Zaktualizowano do $UPDATE_VERSION"
else
  systemctl enable --now deployra-backend
  echo "✔ Gotowe. Kod parowania / stan dołączania: journalctl -u deployra-backend -f"
fi
