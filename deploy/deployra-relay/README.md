# Deployra Relay

Mały kontener `nginx:alpine` (`network_mode: host`, ok. 3 MB RAM) dla maszyn, do których ruch przychodzi tylko
przez Cloudflare po IPv6 (np. mikr.us bez publicznego IPv4). Słucha na `0.0.0.0:80` i `[::]:80` i przekazuje
ruch do usług na `127.0.0.1`.

## Zasada

Relay wpuszcza ruch **tylko do wpisanych domen**. Każdy inny `Host` dostaje 444 (połączenie zamknięte).
Nie ma automatycznego „port bounce” po nazwie hosta: dawny wzorzec `<port>.domena` pozwalał każdemu w internecie
dostać się do dowolnego portu localhost (Docker API, agent, bazy), więc został usunięty.

## Konfiguracja

1. W `nginx.conf`, w `map $host $relay_upstream`, wpisz domeny i porty, np.:
   ```nginx
   node.example.com   127.0.0.1:2750;   # agent Deployry — w panelu adres węzła: https://node.example.com
   tools.example.com  127.0.0.1:8080;
   ```
2. Jeśli korzystasz ze starego schematu `npmpassthrough<id>.<port>.<domena>`, odkomentuj wzorzec w
   `map $host $relay_legacy_port` (z twoją domeną) i wypisz dozwolone porty w `map $relay_legacy_port $relay_legacy_allowed`.
3. `docker compose up -d` (albo Portainer → Stack z `compose.yaml`). Po zmianie konfiguracji: `docker restart deployra-relay`.

Prawdziwe IP odwiedzających jest brane z nagłówka `CF-Connecting-IP` tylko dla adresów Cloudflare (`set_real_ip_from`),
a `X-Forwarded-Proto: https` od Cloudflare jest przekazywane dalej (bez pętli przekierowań HTTP → HTTPS).
