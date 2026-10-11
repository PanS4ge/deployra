#!/bin/sh
# Deployra — aktualizator (sidecar obok panelu / bramki / backendu).
#
#   deployra-updater.sh watch            — czeka na prośbę komponentu (domyślnie)
#   deployra-updater.sh apply <wersja>   — wykonuje aktualizację (w osobnym kontenerze)
#
# Komponent zapisuje /run/deployra-update/request (`version=1.2.0`). Aktualizator odczytuje
# z etykiet swojego kontenera projekt compose, a potem uruchamia osobny, krótko żyjący
# kontener, który robi pull (obrazy z rejestru) albo git + build (instalacja ze źródeł)
# i `docker compose up -d` dla usług z DEPLOYRA_UPDATE_SERVICES. Osobny kontener jest
# potrzebny, bo podmieniamy także samego aktualizatora.
#
# Stan trafia do pliku `status` jako kod kroku (`step=pulling`) — panel tłumaczy go na język
# użytkownika; `message` to tylko szczegóły (np. nazwa taga). Log jest techniczny, po angielsku.
#
# W spoczynku to tylko `sleep` w pętli: kilkaset KB RAM, bez sieci (network_mode: none).
set -u

DIR="${DEPLOYRA_UPDATE_DIR:-/run/deployra-update}"
POLL="${DEPLOYRA_UPDATE_POLL:-5}"
SERVICES="${DEPLOYRA_UPDATE_SERVICES:-}"
IMAGES="${DEPLOYRA_IMAGES:-deployra}"
case "$IMAGES" in
    */*) MODE=image ;;   # ghcr.io/ktoś/deployra → gotowe obrazy z rejestru
    *) MODE=source ;;    # lokalne deployra-* → budujemy z repozytorium git
esac

now() { date +%s; }
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# kv <plik> klucz=wartość… — atomowy zapis pliku stanu (jedna para na linię)
kv() {
    file=$1
    shift
    tmp="$DIR/.$(basename "$file").tmp"
    : >"$tmp"
    for pair in "$@"; do
        printf '%s' "$pair" | tr '\r\n' '  ' >>"$tmp"
        printf '\n' >>"$tmp"
    done
    mv -f "$tmp" "$file"
}

# status <state> <wersja> <krok> [szczegóły]
status() { kv "$DIR/status" "state=$1" "version=$2" "step=$3" "message=${4:-}" "updated=$(now)"; }

valid_version() {
    printf '%s' "$1" | grep -Eq '^v?[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[0-9A-Za-z][0-9A-Za-z.-]{0,30})?$'
}

label() { docker inspect -f "{{ index .Config.Labels \"$2\" }}" "$1" 2>/dev/null; }

# --- tryb watch ------------------------------------------------------------------------------

watch() {
    [ -n "$SERVICES" ] || { log "DEPLOYRA_UPDATE_SERVICES is not set — nothing to update"; exit 2; }
    mkdir -p "$DIR"
    self=$(hostname)   # network_mode: none → hostname to ID kontenera
    until docker version >/dev/null 2>&1; do
        log "Waiting for /var/run/docker.sock…"
        sleep 30
    done
    project=$(label "$self" com.docker.compose.project)
    if [ -z "$project" ]; then
        log "This container wasn't started by docker compose — updates disabled"
        exec sleep 2147483647
    fi
    name=$(docker inspect -f '{{.Name}}' "$self" | sed 's#^/##')
    beat() { kv "$DIR/agent" "mode=$MODE" "project=$project" "services=$SERVICES" "updated=$(now)"; }
    beat
    log "Deployra updater ready: project $project, mode $MODE, services: $SERVICES"

    tick=0
    while :; do
        [ -f "$DIR/request" ] && handle_request
        tick=$((tick + 1))
        if [ "$tick" -ge 12 ]; then
            tick=0
            beat
        fi
        sleep "$POLL"
    done
}

handle_request() {
    version=$(sed -n 's/^version=//p' "$DIR/request" | head -n 1)
    rm -f "$DIR/request"
    if ! valid_version "$version"; then
        status error "$version" bad_version
        return
    fi
    version=${version#v}
    helper="$name-run"
    if [ -n "$(docker ps -q --filter "name=^${helper}\$")" ]; then
        log "An update is already running — ignoring the request for $version"
        return
    fi
    docker rm -f "$helper" >/dev/null 2>&1

    workdir=$(label "$self" com.docker.compose.project.working_dir)
    files=$(label "$self" com.docker.compose.project.config_files)
    envfiles=$(label "$self" com.docker.compose.project.environment_file)
    image=$(docker inspect -f '{{.Image}}' "$self")
    volume=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/run/deployra-update"}}{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}' "$self")
    if [ -z "$workdir" ] || [ -z "$files" ] || [ -z "$volume" ]; then
        status error "$version" no_compose
        return
    fi
    # ze źródeł potrzebujemy całego repozytorium (konteksty budowania to ..), z rejestru — tylko katalogu compose
    mount=$workdir
    [ "$MODE" = source ] && mount=$(dirname "$workdir")

    : >"$DIR/log"
    log "Update to $version requested" >>"$DIR/log"
    status running "$version" launching
    if ! docker run -d --rm --name "$helper" \
        --label deployra.system=updater \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$volume:/run/deployra-update" \
        -v "$mount:$mount" \
        -e DEPLOYRA_UPDATE_SERVICES="$SERVICES" \
        -e DEPLOYRA_IMAGES="$IMAGES" \
        -e UPD_PROJECT="$project" \
        -e UPD_WORKDIR="$workdir" \
        -e UPD_FILES="$files" \
        -e UPD_ENVFILES="$envfiles" \
        -e UPD_IMAGE="$image" \
        "$image" apply "$version" >>"$DIR/log" 2>&1; then
        status error "$version" launch_failed
    fi
}

# --- tryb apply ------------------------------------------------------------------------------

# fail <krok> [szczegóły]
fail() {
    log "✖ $1 ${2:-}"
    status error "$version" "$1" "${2:-}"
    exit 1
}

apply() {
    version=${1:?version required}
    exec >>"$DIR/log" 2>&1
    valid_version "$version" || fail bad_version
    version=${version#v}
    trap 'fail interrupted' TERM INT
    log "Updating to $version — project $UPD_PROJECT, mode $MODE, services: $SERVICES"

    # Pliki .env projektu. Jeśli przypinają DEPLOYRA_VERSION, compose dostaje nową wersję ze środowiska
    # (ma pierwszeństwo przed .env), a sam pin przestawiamy dopiero po udanej aktualizacji.
    envlist=$(printf '%s' "$UPD_ENVFILES" | tr ',' ' ')
    [ -n "$envlist" ] || envlist="$UPD_WORKDIR/.env"
    pins=""
    for f in $envlist; do
        if [ -f "$f" ] && grep -q '^DEPLOYRA_VERSION=' "$f"; then
            pins="$pins $f"
        fi
    done
    [ -z "$pins" ] || export DEPLOYRA_VERSION="$version"

    # Zmienne, z którymi projekt był uruchamiany z powłoki (np. DEPLOYRA_PUBLIC_HOST=… docker compose up),
    # odtwarzamy ze środowiska kontenerów projektu (najpierw aktualizowanych usług). Bierzemy tylko te,
    # do których odwołują się pliki compose; wartości z plików .env mają pierwszeństwo.
    refs=$(cat "$UPD_WORKDIR"/*.yml "$UPD_WORKDIR"/*.yaml 2>/dev/null | grep -o '\${DEPLOYRA_[A-Z0-9_]*' | sed 's/^..//' | sort -u)
    {
        for svc in $SERVICES; do
            docker ps -aq --filter "label=com.docker.compose.project=$UPD_PROJECT" --filter "label=com.docker.compose.service=$svc"
        done
        docker ps -aq --filter "label=com.docker.compose.project=$UPD_PROJECT"
    } | awk '!seen[$0]++' | while read -r c; do
        docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c"
    done | grep '^DEPLOYRA_[A-Z0-9_]*=' | awk -F= '!seen[$1]++' >/tmp/deployra-env
    while IFS= read -r line; do
        key=${line%%=*}
        [ "$key" = DEPLOYRA_VERSION ] && continue
        printf '%s\n' "$refs" | grep -qx "$key" || continue
        pinned=0
        for f in $envlist; do
            [ -f "$f" ] && grep -q "^$key=" "$f" && pinned=1
        done
        [ "$pinned" = 1 ] || export "$line"
    done </tmp/deployra-env

    set -- -p "$UPD_PROJECT" --project-directory "$UPD_WORKDIR"
    old_ifs=$IFS
    IFS=,
    for f in $UPD_FILES; do set -- "$@" -f "$f"; done
    for f in $UPD_ENVFILES; do [ -n "$f" ] && set -- "$@" --env-file "$f"; done
    IFS=$old_ifs

    if [ "$MODE" = source ]; then
        repo=$(dirname "$UPD_WORKDIR")
        owner=$(stat -c '%u:%g' "$repo")
        gitx() {
            # git jako właściciel repozytorium, żeby nie zostawić plików roota w cudzym katalogu
            if [ "$owner" = "0:0" ]; then
                git -c safe.directory='*' -C "$repo" "$@"
            else
                docker run --rm --user "$owner" -e HOME=/tmp -v "$repo:$repo" --entrypoint git "$UPD_IMAGE" \
                    -c safe.directory='*' -C "$repo" "$@"
            fi
        }
        gitx rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail not_git "$repo"
        status running "$version" fetching
        gitx fetch --tags --force --quiet origin || fail fetch_failed
        ref="v$version"
        if ! gitx rev-parse -q --verify "refs/tags/$ref^{commit}" >/dev/null; then
            ref=$version
            gitx rev-parse -q --verify "refs/tags/$ref^{commit}" >/dev/null || fail no_tag "v$version"
        fi
        gitx diff --quiet HEAD -- || fail local_changes "$repo"
        branch=$(gitx symbolic-ref -q --short HEAD || true)
        if [ -n "$branch" ]; then
            gitx merge --ff-only --quiet "$ref" || fail ff_failed "$branch → $ref"
        else
            gitx -c advice.detachedHead=false checkout --quiet "$ref" || fail checkout_failed "$ref"
        fi
        log "Code: $(gitx log -1 --format='%h %s')"
        status running "$version" building
        # shellcheck disable=SC2086
        docker compose "$@" build --pull $SERVICES || fail build_failed
        # shellcheck disable=SC2086
        docker compose "$@" pull --ignore-buildable --quiet $SERVICES || log "(couldn't refresh external images — keeping the current ones)"
    else
        status running "$version" pulling
        # shellcheck disable=SC2086
        docker compose "$@" pull $SERVICES || fail pull_failed
    fi

    status running "$version" starting
    # shellcheck disable=SC2086
    docker compose "$@" up -d --no-deps $SERVICES || fail up_failed
    for f in $pins; do
        sed -i "s/^DEPLOYRA_VERSION=.*/DEPLOYRA_VERSION=$version/" "$f" && log "Pinned version in $f → $version"
    done
    # stare warstwy Deployry tylko zajmują dysk (obrazy aplikacji zostawiamy w spokoju)
    docker image prune -f --filter "label=deployra.component" >/dev/null 2>&1 || true
    log "✔ Updated to $version"
    status done "$version" done
}

case "${1:-watch}" in
    watch) watch ;;
    apply) apply "${2:-}" ;;
    *) echo "usage: $0 [watch | apply <version>]" >&2; exit 2 ;;
esac
