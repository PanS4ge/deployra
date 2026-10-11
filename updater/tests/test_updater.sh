#!/usr/bin/env bash
# Testy aktualizatora bez prawdziwego Dockera: atrapa `docker` zapisuje wywołania,
# a tryb ze źródeł działa na prawdziwym repozytorium git (origin = lokalne repo bare).
#   bash updater/tests/test_updater.sh
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../deployra-updater.sh"
T="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$T"' EXIT
FAILS=0

pass() { printf '  ✔ %s\n' "$1"; }
fail() { printf '  ✖ %s\n' "$1"; FAILS=$((FAILS + 1)); }
check() { if eval "$2"; then pass "$1"; else fail "$1"; fi; }
status_of() { sed -n "s/^$1=//p" "$T/run/status"; }
wait_for() {  # wait_for <warunek> [sekundy]
    for _ in $(seq 1 $((${2:-10} * 10))); do eval "$1" && return 0; sleep 0.1; done
    return 1
}

# --- atrapa docker -----------------------------------------------------------------------
mkdir -p "$T/bin" "$T/run"
cat >"$T/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE_LOG"
case "$1" in
  version|rm) exit 0 ;;
  ps) [ "$2" = "-aq" ] && echo c1; exit 0 ;;
  image) exit 0 ;;
  inspect)
    fmt="$3"
    case "$fmt" in
      *'"com.docker.compose.project"'*) echo deployra-node ;;
      *working_dir*) echo "$FAKE_REPO/deploy" ;;
      *config_files*) echo "$FAKE_REPO/deploy/node.compose.yml" ;;
      *environment_file*) echo "" ;;
      '{{.Name}}') echo /deployra-updater ;;
      '{{.Image}}') echo sha256:abc ;;
      *Mounts*) echo deployra-node_node-updates ;;
      *Config.Env*) printf 'DEPLOYRA_NAME=serwer-z-kontenera\nDEPLOYRA_JOIN=z-kontenera\nDEPLOYRA_DATA=/var/lib/deployra\nPATH=/bin\n' ;;
    esac
    exit 0 ;;
  run)
    # git w kontenerze jako właściciel repo → wykonujemy go lokalnie
    if printf '%s\n' "$@" | grep -qx -- '--entrypoint'; then
      while [ "$1" != "sha256:abc" ]; do shift; done
      shift
      exec git "$@"
    fi
    exit 0 ;;
  compose)
    echo "env DEPLOYRA_NAME=${DEPLOYRA_NAME:-} DEPLOYRA_JOIN=${DEPLOYRA_JOIN:-} DEPLOYRA_VERSION=${DEPLOYRA_VERSION:-}" >>"$FAKE_LOG"
    case " $* " in *" $FAKE_COMPOSE_FAIL "*) exit 1 ;; esac
    exit 0 ;;
esac
exit 0
EOF
printf '#!/bin/sh\necho abc123\n' >"$T/bin/hostname"
chmod +x "$T/bin/docker" "$T/bin/hostname"

export PATH="$T/bin:$PATH" FAKE_LOG="$T/docker.log" FAKE_REPO="$T/repo" FAKE_COMPOSE_FAIL="__nic__"
export DEPLOYRA_UPDATE_DIR="$T/run" DEPLOYRA_UPDATE_POLL=0.1 DEPLOYRA_UPDATE_SERVICES="deployra-backend updater"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# --- repozytorium: origin z tagiem v1.2.0 o commit do przodu ----------------------------------
git init -q -b main "$T/origin-src"
mkdir -p "$T/origin-src/deploy"
printf 'services:\n  deployra-backend:\n    environment:\n      DEPLOYRA_NAME: ${DEPLOYRA_NAME:-dom}\n      DEPLOYRA_JOIN: ${DEPLOYRA_JOIN:-}\n' >"$T/origin-src/deploy/node.compose.yml"
echo "1.0.0" >"$T/origin-src/VERSION"
git -C "$T/origin-src" add . && git -C "$T/origin-src" commit -qm "1.0.0"
git clone -q --bare "$T/origin-src" "$T/origin.git"
git clone -q "$T/origin.git" "$T/repo"
echo "1.2.0" >"$T/origin-src/VERSION"
git -C "$T/origin-src" commit -qam "1.2.0" && git -C "$T/origin-src" tag v1.2.0
git -C "$T/origin-src" push -q "$T/origin.git" main v1.2.0
printf 'DEPLOYRA_JOIN=z-pliku\nDEPLOYRA_VERSION=1.0.0\n' >"$T/repo/deploy/.env"

echo "watch: prośba o aktualizację"
DEPLOYRA_IMAGES=deployra sh "$SCRIPT" watch >"$T/watch.out" 2>&1 &
check "znacznik życia z trybem source" 'wait_for "grep -q mode=source \"$T/run/agent\" 2>/dev/null"'
echo "version=1.2.0" >"$T/run/request"
check "uruchomiony kontener pomocniczy" 'wait_for "grep -q \"^run -d\" \"$FAKE_LOG\""'
run_line=$(grep "^run -d" "$FAKE_LOG")
check "nazwa pomocnika" '[[ "$run_line" == *"--name deployra-updater-run"* ]]'
check "wolumen stanu" '[[ "$run_line" == *"-v deployra-node_node-updates:/run/deployra-update"* ]]'
check "całe repo zamontowane pod tą samą ścieżką" '[[ "$run_line" == *"-v $T/repo:$T/repo"* ]]'
check "argumenty apply" '[[ "$run_line" == *"sha256:abc apply 1.2.0" ]]'
check "status running" '[ "$(status_of state)" = running ] && [ "$(status_of version)" = 1.2.0 ]'
check "prośba zużyta" '[ ! -f "$T/run/request" ]'
echo "version=1.2.0; rm -rf /" >"$T/run/request"
check "zła wersja odrzucona" 'wait_for "[ \"\$(status_of state)\" = error ]"'
kill %1 2>/dev/null

common_env() {
    export UPD_PROJECT=deployra-node UPD_WORKDIR="$T/repo/deploy" UPD_FILES="$T/repo/deploy/node.compose.yml" UPD_ENVFILES="" UPD_IMAGE=sha256:abc
}

echo "apply: obrazy z rejestru"
: >"$FAKE_LOG"
common_env
DEPLOYRA_IMAGES=ghcr.io/ktos/deployra sh "$SCRIPT" apply 1.2.0
check "status done" '[ "$(status_of state)" = done ]'
check "pull + up dla usług" 'grep -q "^compose -p deployra-node --project-directory $T/repo/deploy -f $T/repo/deploy/node.compose.yml pull deployra-backend updater" "$FAKE_LOG" && grep -q "up -d --no-deps deployra-backend updater" "$FAKE_LOG"'
check "zmienne z kontenera, ale .env ma pierwszeństwo" 'grep -q "env DEPLOYRA_NAME=serwer-z-kontenera DEPLOYRA_JOIN= " "$FAKE_LOG"'
check "compose dostaje nową wersję mimo pinu w .env" 'grep -q "DEPLOYRA_VERSION=1.2.0$" "$FAKE_LOG"'
check "pin DEPLOYRA_VERSION przestawiony" 'grep -qx "DEPLOYRA_VERSION=1.2.0" "$T/repo/deploy/.env"'
check "bez builda w trybie image" '! grep -q " build " "$FAKE_LOG"'

echo "apply: błąd pobierania"
FAKE_COMPOSE_FAIL=pull DEPLOYRA_IMAGES=ghcr.io/ktos/deployra sh "$SCRIPT" apply 1.2.1
check "status error z komunikatem" '[ "$(status_of state)" = error ] && status_of message | grep -q "pobieranie"'
check "nieudana aktualizacja nie rusza pinu" 'grep -qx "DEPLOYRA_VERSION=1.2.0" "$T/repo/deploy/.env"'

echo "apply: ze źródeł (git + build)"
: >"$FAKE_LOG"
DEPLOYRA_IMAGES=deployra sh "$SCRIPT" apply v1.2.0
check "status done" '[ "$(status_of state)" = done ]'
check "repo przewinięte do v1.2.0" '[ "$(cat "$T/repo/VERSION")" = 1.2.0 ] && [ "$(git -C "$T/repo" rev-parse HEAD)" = "$(git -C "$T/repo" rev-parse "v1.2.0^{commit}")" ]'
check "nadal na gałęzi main" '[ "$(git -C "$T/repo" symbolic-ref --short HEAD)" = main ]'
check "build przed up" 'grep -n "build --pull deployra-backend updater" "$FAKE_LOG" | cut -d: -f1 | { read b; u=$(grep -n "up -d --no-deps" "$FAKE_LOG" | cut -d: -f1); [ "$b" -lt "$u" ]; }'

echo "apply: brak taga i lokalne zmiany"
DEPLOYRA_IMAGES=deployra sh "$SCRIPT" apply 9.9.9
check "brak taga → error" '[ "$(status_of state)" = error ] && status_of message | grep -q "taga"'
git -C "$T/origin-src" commit -q --allow-empty -m "1.3.0" && git -C "$T/origin-src" tag v1.3.0 && git -C "$T/origin-src" push -q "$T/origin.git" main v1.3.0
echo "zmiana" >>"$T/repo/VERSION"
DEPLOYRA_IMAGES=deployra sh "$SCRIPT" apply 1.3.0
check "lokalne zmiany → error" '[ "$(status_of state)" = error ] && status_of message | grep -q "lokalne zmiany"'

if [ "$FAILS" -gt 0 ]; then
    echo "Niepowodzenia: $FAILS"
    echo "--- log aktualizatora ---"; cat "$T/run/log" 2>/dev/null
    exit 1
fi
echo "OK"
