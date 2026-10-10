#!/bin/sh
# Deployra-WG: wykonuje zlecenia przeładowania konfiguracji.
# Montowany do /docker-entrypoint.d/ oficjalnego obrazu nginx (obraz bez zmian).
# Bramka zapisuje ctl/<id>.request, my odpowiadamy ctl/<id>.result:
#   "ok"                      — nginx -t przeszedł, wykonano reload
#   "error\n<wyjście nginx>"  — konfiguracja odrzucona, nginx działa na starej

CTL=/etc/nginx/deployra/ctl
mkdir -p "$CTL" /etc/nginx/deployra/http /etc/nginx/deployra/stream /etc/nginx/deployra/access /var/cache/nginx/deployra

(
    while :; do
        for req in "$CTL"/*.request; do
            [ -e "$req" ] || continue
            id=$(basename "$req" .request)
            if out=$(nginx -t 2>&1); then
                if nginx -s reload 2>/tmp/deployra-reload.err; then
                    printf 'ok\n' > "$CTL/.$id.tmp"
                else
                    printf 'error\n%s\n' "$(cat /tmp/deployra-reload.err)" > "$CTL/.$id.tmp"
                fi
            else
                printf 'error\n%s\n' "$out" > "$CTL/.$id.tmp"
            fi
            mv "$CTL/.$id.tmp" "$CTL/$id.result"
            rm -f "$req"
        done
        sleep 1
    done
) &

echo "deployra-reloader: nasłuchuję zleceń w $CTL"
