#!/bin/sh
# Deployra-WG: wykonuje zlecenia przeładowania konfiguracji.
# Bramka kopiuje ten skrypt do wolumenu (base/), a kontener nginx (oficjalny obraz, bez zmian)
# uruchamia go przed startem nginx — zob. deploy/gateway.compose.yml.
# Bramka zapisuje ctl/<id>.request, my odpowiadamy ctl/<id>.result:
#   "ok"                      — nginx -t przeszedł, wykonano reload
#   "error\n<wyjście nginx>"  — konfiguracja odrzucona, nginx działa na starej

CTL=/etc/nginx/deployra/ctl
mkdir -p "$CTL" /etc/nginx/deployra/http /etc/nginx/deployra/stream /etc/nginx/deployra/access /var/cache/nginx/deployra

# Możliwości nginx dla bramki: moduł HTTP/3 (quic) jest w oficjalnym obrazie od 1.25; bez niego bramka nie renderuje quic
if nginx -V 2>&1 | grep -q -- '--with-http_v3_module'; then echo 1 > /etc/nginx/deployra/base/http3; else echo 0 > /etc/nginx/deployra/base/http3; fi

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
