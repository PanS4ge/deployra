# Research: stos Deployry (import NPM, lekki UI, WireGuard, TLS, pliki, terminal, Git)

> Stan na 2026-10-10. Źródła pierwotne (repozytoria upstream, oficjalna dokumentacja, PyPI/npm, specyfikacje),
> czytane bezpośrednio. Rozmiary JS zmierzone z plików unpkg (surowo / `gzip -9`).
> Co nie zostało potwierdzone w źródle pierwotnym, oznaczono **[niezweryfikowane]**.

## Skrót decyzji dla Deployry

| Temat | Decyzja | Najważniejsze źródło |
|---|---|---|
| Import NPM | Czytamy `database.sqlite` (read-only) + `/data/access/*` + `/data/custom_ssl/npm-*` + `/etc/letsencrypt/live/npm-*`; REST API tylko jako fallback (nie zwraca haseł basic-auth) | [npm-acl], [npm-cert] |
| UI | Jinja2 + htmx 2.0.11 + Alpine 3.17.4, pliki zvendorowane; SSE natywnie przez `fastapi.sse` (FastAPI ≥ 0.135); instalować `fastapi uvicorn jinja2 python-multipart`, nie `fastapi[standard]` | [fa-sse], [fa-pypi] |
| Tunel | Kernel WG + `NET_ADMIN`; fallback `wireguard-go` z `/dev/net/tun`; klucze z `cryptography` X25519 (bajtowo zgodne z `wg genkey`); zmiany peerów przez `wg set` / `wg syncconf` | [wg8], [wgq-bash], [ossl-ecx] |
| TLS | nginx zostaje; `lego` na żądanie (HTTP-01 webroot lub Cloudflare DNS-01 dla wildcardów), potem `nginx -t && nginx -s reload` | [lego-cf], [le-challenges] |
| Pliki | SFTP w procesie (asyncssh) z chrootem per strona; FTP wylatuje | [asyncssh-server], [rfc2577] |
| Stacki / build | `docker compose` i `docker build --progress=plain` przez CLI; Engine API nie ma compose, a docker-py nie wspiera BuildKit | [engine-swagger], [dpy-2230] |
| Terminal | `@xterm/xterm` 6.0.0 ładowany tylko na stronie terminala; docker-py `exec_create(tty=True)` + `exec_start(socket=True)` | [dpy-exec] |

## 0. Aktualne stabilne wersje

| Komponent | Wersja | Źródło |
|---|---|---|
| Nginx Proxy Manager | v2.16.0 (2026-09-24) | [npm-rel] |
| htmx | 2.0.11 (`latest`; 4.0.0 tylko jako `next`, stanie się `latest` w 2027) | [htmx-npm], [htmx-home] |
| htmx-ext-sse | 2.2.4 | [htmx-sse] |
| Alpine.js | 3.17.4 | [alpine-npm] |
| xterm.js | `@xterm/xterm` 6.0.0 (stary pakiet `xterm` jest deprecated) | [xterm-npm] |
| FastAPI / Jinja2 | 0.143.0 / 3.1.6 | [fa-pypi], [jinja-pypi] |
| docker-py | 7.2.0 | [dpy-pypi] |
| cryptography / asyncssh | 50.0.2 / 2.24.1 | [crypto-pypi], [asyncssh-pypi] |
| lego / acme.sh / certbot / Caddy | v5.5.2 / 3.1.6 / 5.8.0 / v2.11.7 | [lego-rel], [acmesh-rel], [certbot-pypi], [caddy-rel] |
| nginx | 1.31.6 mainline, 1.30.5 stable | [nginx-download] |

## 1. Nginx Proxy Manager — model danych i API (v2.16.0)

### 1.1 Gdzie NPM trzyma dane

| Element | Lokalizacja | Źródło |
|---|---|---|
| SQLite | `DB_SQLITE_FILE`, domyślnie `/data/database.sqlite` | [npm-config] |
| MySQL/MariaDB | `DB_MYSQL_HOST/PORT/USER/PASSWORD/NAME` (+ `DB_MYSQL_SSL*`); ma pierwszeństwo przed SQLite | [npm-config], [npm-setup] |
| Postgres | `DB_POSTGRES_HOST/PORT/USER/PASSWORD/NAME` | [npm-config] |
| Wygenerowane konfigi | `/data/nginx/{proxy_host,redirection_host,dead_host,stream}/<id>.conf`; błędne jako `<id>.conf.err` | [npm-nginx], [npm-paths] |
| htpasswd | `/data/access/<access_list_id>` (`openssl passwd -apr1`) | [npm-acl] |
| Własne certy | `/data/custom_ssl/npm-<id>/{fullchain.pem,privkey.pem}` | [npm-cert] |
| Let's Encrypt | `/etc/letsencrypt/live/npm-<id>/` (symlinki — rozwiązywać `realpath`) | [npm-cert], [certbot-where] |
| Wersja schematu | tabela Knex `migrations` | [npm-db] |

`certificate.provider = letsencrypt` → `/etc/letsencrypt/live/npm-{id}/…`, w przeciwnym razie (`other`) → `/data/custom_ssl/npm-{id}/…` [npm-tpl-cert]. PEM-y własnych certów są też w DB: `certificate.meta.certificate`, `meta.certificate_key`, `meta.intermediate_certificate` [npm-cert].

### 1.2 Tabele (kolumny JSON **pogrubione**)

| Tabela | Kolumny | is_deleted | enabled |
|---|---|---|---|
| `proxy_host` | id, created_on, modified_on, owner_user_id, **domain_names**, forward_host, forward_port, forward_scheme, access_list_id, certificate_id, ssl_forced, caching_enabled, block_exploits, allow_websocket_upgrade, http2_support, hsts_enabled, hsts_subdomains, trust_forwarded_proto (od 2026-01-31), advanced_config, **meta**, **locations** | tak | tak |
| `redirection_host` | …, **domain_names**, forward_domain_name, forward_scheme (domyślnie `auto`), forward_http_code (302), preserve_path, certificate_id, ssl_forced, block_exploits, http2_support, hsts_enabled, hsts_subdomains, advanced_config, **meta** | tak | tak |
| `dead_host` | …, **domain_names**, certificate_id, ssl_forced, http2_support, hsts_enabled, hsts_subdomains, advanced_config, **meta** | tak | tak |
| `stream` | …, incoming_port, forwarding_host, forwarding_port, tcp_forwarding, udp_forwarding, certificate_id (od 2024-04-27), **meta** | tak | tak |
| `certificate` | …, provider, nice_name, **domain_names**, expires_on, **meta** | tak | – |
| `access_list` | …, name, satisfy_any, pass_auth, **meta** | tak | – |
| `access_list_auth` | id, access_list_id, username, password (**jawny tekst**), **meta** | **nie** | – |
| `access_list_client` | id, access_list_id, address, directive (allow/deny), **meta** | **nie** | – |
| `user` / `auth` | email, name, **roles** / secret (bcrypt), **meta** (TOTP) | tak | – |

Źródła: [npm-m-init], [npm-migrations], [npm-models], [npm-acl], [npm-m-auth], [npm-2fa].

- `proxy_host.locations[]`: `{id?, path, forward_scheme, forward_host, forward_port, forward_path?, advanced_config?, access_list_id}`; `forward_host` z `/` NPM rozbija na host + ścieżkę [npm-s-ph], [npm-nginx].
- `certificate.meta`: `dns_challenge`, `dns_provider`, `dns_provider_credentials`, `propagation_seconds`, `key_type`, `letsencrypt_certificate` [npm-s-cert].
- W SQLite kolumny JSON to tekst (`'["a.example"]'`) → `json.loads`; booleany jako 0/1 [npm-models]. Znaczniki czasu w czasie lokalnym kontenera, bez strefy [npm-now].
- Starsze instalacje v2 nie mają części kolumn → wykrywać przez `PRAGMA table_info` [npm-migrations].

### 1.3 REST API

- Wszystko pod `/api/…`: `/tokens`, `/nginx/{proxy-hosts,redirection-hosts,dead-hosts,streams,access-lists,certificates}` [npm-r-main].
- Logowanie: `POST /api/tokens {"identity": email, "secret": hasło}` → `{token, expires}` albo `{requires_2fa, challenge_token}` → `POST /api/tokens/2fa {challenge_token, code}` [npm-s-tokens], [npm-r-tokens]. Potem `Authorization: Bearer <token>`.
- `?expand=`: proxy hosts `access_list,owner,certificate`; access lists `owner,items,clients,proxy_hosts` [npm-s-ph-get], [npm-s-acl-get].
- **API ukrywa** hasła basic-auth (`password: ""`) i `meta.dns_provider_credentials` [npm-acl], [npm-cert].
- `GET /api/nginx/certificates/{id}/download` → zip PEM-ów, **tylko dla Let's Encrypt** [npm-r-cert], [npm-cert].

### 1.4 Rekomendacja dla importera

Czytać bezpośrednio DB i pliki: `sqlite3.connect("file:/data/database.sqlite?mode=ro", uri=True)` [py-sqlite-uri], filtrować `is_deleted = 0` (tabele podrzędne list dostępu nie mają tej kolumny). Hasła basic-auth są tylko w DB (jawnie) lub jako apr1 w `/data/access/<id>`, które nginx przyjmuje bez zmian w `auth_basic_user_file` [nginx-authbasic]. W Pythonie 3.13 nie ma modułu `crypt` [py-crypt] → nowe wpisy htpasswd przez `bcrypt`/`openssl passwd -apr1`.

## 2. Lekki UI zamiast NiceGUI

- **Koszt NiceGUI**: Vue/Quasar + socket.io, WebSocket otwarty po załadowaniu strony, każdy event idzie do Pythona [ng-readme]; stan strony jest prywatny dla karty przeglądarki [ng-page]; każdy `Client` trzyma po stronie serwera własne `elements` i `outbox` [ng-client]. Liczby RAM na kartę brak w źródłach **[niezweryfikowane — zmierzyć]**.
- **Opcja A (wybrana)**: Jinja2 + htmx + Alpine. htmx 2.0.11 `htmx.min.js` 52 182 B (16 944 B gz) [unpkg-htmx]; SSE ext 2 853 B (1 073 B gz) [unpkg-sse]; Alpine 3.17.4 55 891 B (19 992 B gz) [unpkg-alpine]. Polling `hx-trigger="every 2s [!document.hidden]"` [htmx-trigger]; SSE `hx-ext="sse" sse-connect sse-swap` [htmx-sse]. htmx odradza CDN w produkcji [htmx-sse].
- **Opcja B**: Preact + `htm` bez buildu jest możliwa [preact-gs]; Svelte zawsze wymaga kompilatora [svelte-readme]. SPA = podwójne API i stan → odrzucone.
- **SSE w FastAPI**: od 0.135.0 `from fastapi.sse import EventSourceResponse, ServerSentEvent`; sync generatory nie blokują pętli; `raw_data=` do HTML; ping co 15 s, nagłówki `Cache-Control: no-cache` i `X-Accel-Buffering: no` [fa-sse], [fa-sse-ex].
- Bez HTTP/2 przeglądarka ma limit 6 połączeń SSE na domenę [mdn-es] → panel za HTTP/2 lub jeden strumień na stronę.

## 3. WireGuard hub-and-spoke

- Hub (VPS): `ListenPort = 51820`, `[Peer]` per węzeł z `AllowedIPs = <ip>/32` (+ podsieć LAN, jeśli routujemy). AllowedIPs to jednocześnie filtr źródła i trasa [wg8].
- Węzeł za NAT: `Endpoint = vps:51820`, `PersistentKeepalive = 25` („sensowny interwał… 25 sekund”) [wg-quick-start], [wg8].
- Routing hostów LAN za węzłem: `net.ipv4.ip_forward=1` [kernel-sysctl] + MASQUERADE, np. `iptables -t nat -A POSTROUTING -o eth+ -j MASQUERADE` [lsio-server]; `PostUp/PostDown` w bashu, `%i` = interfejs [wgq8].
- Docker: kernel WG od Linux 5.6 [kn-56]; `cap_add: NET_ADMIN`, `SYS_MODULE` tylko do ładowania modułów [lsio-wg]. `src_valid_mark=1` potrzebne tylko gdy AllowedIPs zawiera `/0` [wgq-bash].
- Fallback userspace: gdy `ip link add … type wireguard` się nie uda, wg-quick uruchamia `${WG_QUICK_USERSPACE_IMPLEMENTATION:-wireguard-go}` [wgq-bash]; wireguard-go otwiera `/dev/net/tun` [wg-go-tun]; w compose `devices: [/dev/net/tun]` [compose-devices]. Dostępność TUN na OpenVZ/LXC zależy od dostawcy **[niezweryfikowane]**.
- Klucze w Pythonie: WG = Curve25519, 32 B, base64 [wg-protocol], [wg8]; `wg genkey` klampuje bajty jak RFC 7748 [wg-genkey], [rfc7748]; OpenSSL (backend `cryptography`) klampuje tak samo [ossl-ecx] → `X25519PrivateKey.generate().private_bytes_raw()` jest zgodne z `wg genkey` [crypto-x25519]. PSK = 32 losowe bajty (`wg genpsk`).
- Proxy do domu: `proxy_pass http://10.x.x.N:PORT`; kontener proxy musi widzieć `wg0` — `network_mode: "service:<wg>"` lub `host` [compose-netmode], [docker-net-container].
- Zmiany na żywo: `wg set wg0 peer … allowed-ips …`, `wg syncconf wg0 <(wg-quick strip wg0)` nie zrywa sesji [wg8], [wgq8].

## 4. TLS bez stałego kontenera certbota

Wildcard wymaga DNS-01 [le-challenges]; certy LE żyją 90 dni, odnawiać co ~60 [le-faq].

| Narzędzie | Rozmiar | Cloudflare DNS-01 | Uwagi |
|---|---|---|---|
| **lego** | jedna binarka, tarball 21,2 MB [lego-rel] | `CF_DNS_API_TOKEN` (Zone:Read + DNS:Edit) [lego-cf] | `--http.webroot` [lego-options]; `renew` domyślnie 30 dni przed wygaśnięciem, `--renew-hook` [lego-renew]; pliki `.lego/certificates/<domena>.{crt,key,issuer.crt,json}`, wildcard jako `_.example.com.*` [lego-obtain] |
| acme.sh | czysty shell | `dns_cf` + `CF_Token` [acmesh-dnsapi] | **domyślny CA to ZeroSSL** [acmesh-src] |
| certbot | Python + wiele zależności [certbot-pypi] | wtyczka `certbot-dns-cloudflare` | najcięższy |
| Caddy | binarka 18,1 MB [caddy-rel] | moduł `caddy-dns/cloudflare` wymaga własnego buildu (`xcaddy`) [caddy-dns-cf], [caddy-build] | auto HTTPS [caddy-autohttps], API `localhost:2019` z `POST /load` (atomowo, z rollbackiem) [caddy-api]; brak porównania RAM z nginx w źródłach **[niezweryfikowane]** |

Werdykt: nginx + lego na żądanie. Reload nginx jest łagodny i przy błędzie zostaje przy starej konfiguracji [nginx-beg]. Caddy rozważyć tylko przy rezygnacji ze zgodności ze snippetami nginx (NPM `advanced_config`) i ze streamami TCP/UDP.

## 5. Wgrywanie plików i Compose

| Opcja | Użytkownicy w locie | Izolacja | Uwagi |
|---|---|---|---|
| atmoz/sftp | nie (konfiguracja przy starcie) [atmoz] | chroot OpenSSH (korzeń root-owned, upload do podkatalogu) [sshd-config] | — |
| SFTPGo | tak, REST API [sftpgo-api] | wirtualne korzenie | AGPL-3.0, osobna binarka [sftpgo] |
| **asyncssh w procesie** | tak: `validate_password` / `validate_public_key` per połączenie [asyncssh-server] | `SFTPServer(chan, chroot=path)`; uwaga: istniejące symlinki wychodzące poza chroot są podążane [asyncssh-sftp], [asyncssh-ex] | zależy tylko od `cryptography` [asyncssh-pypi] |

FTP przesyła hasła jawnym tekstem [rfc2577] i wymaga zakresu portów pasywnych [delfer].

Compose: Engine API (v1.56) nie ma endpointów compose [engine-swagger]; compose to klient (Go CLI / Go SDK) [compose-history], [compose-sdk]; zasoby grupuje etykieta `com.docker.compose.project` [compose-model] → shell-out `docker compose -p <nazwa> -f … up -d`.

## 6. Terminal, build z Gita, webhooki

- xterm.js 6.0.0: `lib/xterm.js` 488 663 B (120 808 B gz), `xterm.css` 7 112 B; `addon-fit` 1 521 B [unpkg-xterm], [unpkg-xterm-addons]; można vendorować bez buildu [xterm-readme].
- docker-py: `APIClient.exec_create(cid, cmd, stdin=True, tty=True)` → `exec_start(id, tty=True, socket=True)` zwraca socket (zamyka wywołujący), `exec_resize(id, height, width)` [dpy-exec]; na unix-sockecie surowy `SocketIO`, do `sendall/recv` używać `._sock` [dpy-client]. Z TTY strumień to surowe bajty PTY [engine-swagger].
- WebSocket: sprawdzać `Origin` [rfc6455]; nginx: `proxy_set_header Upgrade $http_upgrade; Connection "upgrade"` [nginx-ws].
- `client.images.build()` czyta cały strumień przed zwrotem — nie da się streamować [dpy-images]; `client.api.build(decode=True)` zwraca dicty [dpy-build]; docker-py nie wspiera BuildKit [dpy-2230], a klasyczny builder jest deprecated od Docker 23.0 [docker-deprecated] → `docker build --progress=plain`.
- Webhooki: GitHub `X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(secret, surowe body)`, porównanie `hmac.compare_digest` [gh-validate]; Gitea wysyła `X-Gitea-Signature` i `X-Hub-Signature-256` [gitea-deliver] → jeden weryfikator.

## Źródła

[npm-rel]: https://github.com/NginxProxyManager/nginx-proxy-manager/releases/tag/v2.16.0
[npm-config]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/lib/config.js
[npm-setup]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/docs/src/setup/index.md
[npm-m-init]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/migrations/20180618015850_initial.js
[npm-migrations]: https://github.com/NginxProxyManager/nginx-proxy-manager/tree/v2.16.0/backend/migrations
[npm-models]: https://github.com/NginxProxyManager/nginx-proxy-manager/tree/v2.16.0/backend/models
[npm-m-auth]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/models/auth.js
[npm-now]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/models/now_helper.js
[npm-db]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/db.js
[npm-cert]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/internal/certificate.js
[npm-tpl-cert]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/templates/_certificates.conf
[npm-nginx]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/internal/nginx.js
[npm-paths]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/docker/rootfs/etc/s6-overlay/s6-rc.d/prepare/20-paths.sh
[npm-acl]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/internal/access-list.js
[npm-2fa]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/internal/2fa.js
[npm-r-main]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/routes/main.js
[npm-r-tokens]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/routes/tokens.js
[npm-r-cert]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/routes/nginx/certificates.js
[npm-s-tokens]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/schema/paths/tokens/post.json
[npm-s-ph-get]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/schema/paths/nginx/proxy-hosts/get.json
[npm-s-ph]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/schema/components/proxy-host-object.json
[npm-s-cert]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/schema/components/certificate-object.json
[npm-s-acl-get]: https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/schema/paths/nginx/access-lists/get.json
[certbot-where]: https://eff-certbot.readthedocs.io/en/stable/using.html#where-are-my-certificates
[py-sqlite-uri]: https://docs.python.org/3.13/library/sqlite3.html#sqlite3-uri-tricks
[py-crypt]: https://docs.python.org/3.13/library/crypt.html
[nginx-authbasic]: https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html#auth_basic_user_file
[ng-readme]: https://github.com/zauberzeug/nicegui/blob/main/README.md
[ng-client]: https://github.com/zauberzeug/nicegui/blob/main/nicegui/client.py
[ng-page]: https://github.com/zauberzeug/nicegui/blob/main/website/documentation/content/page_documentation.py
[htmx-home]: https://htmx.org/
[htmx-npm]: https://www.npmjs.com/package/htmx.org
[htmx-trigger]: https://htmx.org/attributes/hx-trigger/
[htmx-sse]: https://htmx.org/extensions/sse/
[unpkg-htmx]: https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js
[unpkg-sse]: https://unpkg.com/htmx-ext-sse@2.2.4/dist/sse.min.js
[alpine-npm]: https://www.npmjs.com/package/alpinejs
[unpkg-alpine]: https://unpkg.com/alpinejs@3.17.4/dist/cdn.min.js
[preact-gs]: https://preactjs.com/guide/v11/getting-started
[svelte-readme]: https://github.com/sveltejs/svelte/blob/main/README.md
[fa-pypi]: https://pypi.org/project/fastapi/
[jinja-pypi]: https://pypi.org/project/Jinja2/
[fa-sse]: https://fastapi.tiangolo.com/tutorial/server-sent-events/
[fa-sse-ex]: https://github.com/fastapi/fastapi/blob/master/docs_src/server_sent_events/tutorial003_py310.py
[mdn-es]: https://developer.mozilla.org/en-US/docs/Web/API/EventSource
[nginx-ws]: https://nginx.org/en/docs/http/websocket.html
[nginx-beg]: https://nginx.org/en/docs/beginners_guide.html
[nginx-download]: https://nginx.org/en/download.html
[dpy-pypi]: https://pypi.org/project/docker/
[dpy-exec]: https://github.com/docker/docker-py/blob/main/docker/api/exec_api.py
[dpy-client]: https://github.com/docker/docker-py/blob/main/docker/api/client.py
[dpy-images]: https://github.com/docker/docker-py/blob/main/docker/models/images.py
[dpy-build]: https://github.com/docker/docker-py/blob/main/docker/api/build.py
[dpy-2230]: https://github.com/docker/docker-py/issues/2230
[docker-deprecated]: https://github.com/docker/cli/blob/master/docs/deprecated.md#legacy-builder-for-linux-images
[wg-quick-start]: https://www.wireguard.com/quickstart/
[wg-protocol]: https://www.wireguard.com/protocol/
[wg8]: https://git.zx2c4.com/wireguard-tools/about/src/man/wg.8
[wgq8]: https://git.zx2c4.com/wireguard-tools/about/src/man/wg-quick.8
[wgq-bash]: https://git.zx2c4.com/wireguard-tools/tree/src/wg-quick/linux.bash
[wg-genkey]: https://git.zx2c4.com/wireguard-tools/tree/src/genkey.c
[wg-go-tun]: https://git.zx2c4.com/wireguard-go/tree/tun/tun_linux.go
[rfc7748]: https://www.rfc-editor.org/rfc/rfc7748#section-5
[ossl-ecx]: https://github.com/openssl/openssl/blob/master/providers/implementations/keymgmt/ecx_kmgmt.c
[crypto-x25519]: https://cryptography.io/en/latest/hazmat/primitives/asymmetric/x25519/
[crypto-pypi]: https://pypi.org/project/cryptography/
[lsio-wg]: https://github.com/linuxserver/docker-wireguard/blob/master/README.md
[lsio-server]: https://github.com/linuxserver/docker-wireguard/blob/master/root/defaults/server.conf
[kn-56]: https://kernelnewbies.org/Linux_5.6
[kernel-sysctl]: https://docs.kernel.org/networking/ip-sysctl.html
[compose-netmode]: https://github.com/compose-spec/compose-spec/blob/main/05-services.md#network_mode
[compose-devices]: https://github.com/compose-spec/compose-spec/blob/main/05-services.md#devices
[docker-net-container]: https://docs.docker.com/engine/network/#container-networks
[le-challenges]: https://letsencrypt.org/docs/challenge-types/
[le-faq]: https://letsencrypt.org/docs/faq/
[lego-rel]: https://github.com/go-acme/lego/releases/tag/v5.5.2
[lego-cf]: https://github.com/go-acme/lego/blob/master/providers/dns/cloudflare/cloudflare.toml
[lego-obtain]: https://github.com/go-acme/lego/blob/master/docs/content/usage/cli/Obtain-a-Certificate.md
[lego-options]: https://github.com/go-acme/lego/blob/master/docs/content/usage/cli/Options.md
[lego-renew]: https://github.com/go-acme/lego/blob/master/docs/content/usage/cli/Renew-a-Certificate.md
[acmesh-rel]: https://github.com/acmesh-official/acme.sh/releases/tag/3.1.6
[acmesh-src]: https://github.com/acmesh-official/acme.sh/blob/master/acme.sh
[acmesh-dnsapi]: https://github.com/acmesh-official/acme.sh/wiki/dnsapi
[certbot-pypi]: https://pypi.org/project/certbot/
[caddy-rel]: https://github.com/caddyserver/caddy/releases/tag/v2.11.7
[caddy-api]: https://caddyserver.com/docs/api
[caddy-autohttps]: https://caddyserver.com/docs/automatic-https
[caddy-build]: https://caddyserver.com/docs/build
[caddy-dns-cf]: https://github.com/caddy-dns/cloudflare
[rfc2577]: https://www.rfc-editor.org/rfc/rfc2577
[delfer]: https://github.com/delfer/docker-alpine-ftp-server
[atmoz]: https://github.com/atmoz/sftp
[sshd-config]: https://man.openbsd.org/sshd_config.5
[sftpgo]: https://github.com/drakkan/sftpgo
[sftpgo-api]: https://github.com/drakkan/sftpgo/blob/main/openapi/openapi.yaml
[asyncssh-pypi]: https://pypi.org/project/asyncssh/
[asyncssh-sftp]: https://github.com/ronf/asyncssh/blob/develop/asyncssh/sftp.py
[asyncssh-server]: https://github.com/ronf/asyncssh/blob/develop/asyncssh/server.py
[asyncssh-ex]: https://github.com/ronf/asyncssh/blob/develop/examples/chroot_sftp_server.py
[engine-swagger]: https://github.com/moby/moby/blob/master/api/swagger.yaml
[compose-history]: https://docs.docker.com/compose/intro/history/
[compose-sdk]: https://docs.docker.com/compose/compose-sdk/
[compose-model]: https://docs.docker.com/compose/intro/compose-application-model/
[xterm-npm]: https://www.npmjs.com/package/@xterm/xterm
[xterm-readme]: https://github.com/xtermjs/xterm.js/blob/master/README.md
[unpkg-xterm]: https://unpkg.com/@xterm/xterm@6.0.0/lib/xterm.js
[unpkg-xterm-addons]: https://unpkg.com/@xterm/addon-attach@0.12.0/lib/addon-attach.js
[rfc6455]: https://www.rfc-editor.org/rfc/rfc6455#section-10.2
[gh-validate]: https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
[gitea-deliver]: https://github.com/go-gitea/gitea/blob/main/services/webhook/deliver.go
