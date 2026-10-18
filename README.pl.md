# Deployra ✦

[English](README.md) · **Polski**

**Coolify dla LowEndów.** Jedno miejsce do deployu: aplikacja → domena → DNS → certyfikat → tunel do domu.
Zastępuje zestaw *strona z FTP + Portainer + Nginx Proxy Manager (×2 z WireGuardem)* i działa na 512 MB RAM.

```
                      Internet (80/443 albo Cloudflare Tunnel)
                                   │
 ┌──────────── VPS ────────────────▼──────────────────────────┐
 │ Deployra-WG   hub WireGuard 10.66.0.1 · nginx · lego · cloudflared │
 └──────┬──────────────────────────────┬──────────────────────┘
        │ tunel WG                     │ tunel WG
 ┌──────▼─────────────┐        ┌───────▼──────────────────────┐
 │ Deployra (panel)   │──wg──► │ Deployra Backend 10.66.0.N   │
 │ gdziekolwiek       │ HMAC   │ Docker, stacki, aplikacje,   │
 └────────────────────┘        │ Git, SFTP, terminal, Proxmox │
                               └──────────────────────────────┘
```

Ruch publiczny idzie **jednym skokiem**: `nginx na bramce → wg0 → węzeł → kontener` (zamiast NPM → NPM-z-WG → host).

## Co potrafi

| Obszar | Funkcje |
|---|---|
| **Aplikacje** | kreator: obraz Dockera, docker-compose, strona statyczna (ZIP + SFTP), repozytorium Git (build z Dockerfile, webhook GitHub/Gitea) · domena, rekord DNS w Cloudflare, Let's Encrypt i test przez tunel w jednym kroku · rollback · opcjonalnie **bez przestojów** · zmienne na etapie budowania, edytor `.env`, zmienne współdzielone · zadania cykliczne (cron) w kontenerze |
| **Docker** (zamiast Portainera) | kontenery, logi i statystyki na żywo, terminal w przeglądarce, obrazy, wolumeny, sieci, stacki compose · menedżer plików (także całe foldery: wgrywanie i pobieranie jako ZIP) · szybkie akcje dla baz i webserwerów (zrzut SQL, VACUUM, procesy, `df -h`) |
| **Proxmox** | backend na hoście Proxmoxa pokazuje **każdy kontener LXC z Dockerem** — bez instalowania agenta w każdym CT |
| **Proxy** (zamiast NPM) | hosty proxy, przekierowania, hosty 404, streamy TCP/UDP, listy dostępu, własne lokacje, surowy `advanced` nginx z automatycznym rollbackiem · HTTP/3 (QUIC) · **logowanie Deployra** przed dowolną aplikacją |
| **Import z NPM** | z pliku `database.sqlite` (razem z hasłami list dostępu) albo przez API; mapowanie starych adresów na węzły; certyfikaty przenoszone albo wystawiane na nowo |
| **Tunel** | hub WireGuard, dołączanie węzłów tokenem, routing podsieci LAN (NAS, Home Assistant…), węzły widzą się nawzajem · kilka bramek, także czysty nginx bez WireGuarda · węzły za **zewnętrznym WireGuardem** (wg-easy) |
| **Cloudflare** | rekordy A/AAAA per host (bez wildcarda), certyfikaty wildcard (DNS-01), **Cloudflare Tunnel** (cloudflared — bez publicznego IP i otwartych portów) |
| **Odwiedziny** | unikalni i powracający per domena, wykresy jak w Cloudflare — bez ciasteczek, bez botów, liczone na bramce z logów nginx |
| **Ruch i bezpieczeństwo** | topka adresów IP z krajem i ISP (DB-IP Lite), podejrzane adresy (zalew zapytań, skanery, szukanie dziur), bany w nginx i firewallu (ipset), opcjonalny auto-ban z białą listą |
| **Uptime** (à la Uptime Kuma) | monitory aplikacji, kontenerów, węzłów i URL, uptime 24 h / 7 dni / 30 dni, powiadomienia (panel, Discord, ntfy, Slack) |
| **Panel** | FastAPI + Jinja2 + htmx + Alpine (bez NiceGUI, bez kroku build), ciemny kosmiczny motyw, użytkownicy i uprawnienia, SQLite albo MySQL/MariaDB |
| **Języki** | polski i angielski — każdy użytkownik wybiera swój w menu konta; nowy język to jeden plik JSON |
| **Aplikacje z plików** | wrzucasz ZIP (albo SFTP), a Deployra to uruchamia: PHP + Apache, Python (skrypt, Flask, FastAPI, Django), Node.js (skrypt, Express, Next/Nuxt/Astro), Bun, Deno, Go, Rust, Ruby, Java (.jar), .NET, shell — zależności instalują się same |
| **Zasoby i alerty** | historia CPU / RAM / dysku węzłów i aplikacji, progi z czasem trwania, wykrywanie OOM i pętli restartów, powiadomienia do panelu, webhooka, Discorda, Slacka, Telegrama, ntfy, Gotify, Pushovera albo e-mailem — kanały wybierasz osobno dla każdej reguły |
| **Przenoszenie aplikacji** | jednym kliknięciem aplikacja z plikami, wolumenami i kontami SFTP jedzie na inny węzeł, domena przełącza się sama, nieudane przenosiny się wycofują; opcjonalnie automatycznie z węzła, któremu kończy się RAM |
| **Kopie zapasowe** | stan panelu i **bazy danych aplikacji** (PostgreSQL, MySQL/MariaDB, MongoDB) według harmonogramu na S3/R2/MinIO, SFTP, FTP albo dysk — z retencją, pobieraniem i przywracaniem |
| **Mapa** | graf całej infrastruktury: panel → bramki → węzły → kontenery LXC → aplikacje → domeny, ze stanem każdego elementu |
| **Pluginy** | szablony aplikacji do kreatora (wbudowane: Uptime Kuma, Vaultwarden, Gitea, n8n, Grafana, WordPress…), paczki językowe, kod Pythona z własnymi stronami i obsługą zdarzeń |
| **Aktualizacje** | panel sprawdza wydania, pokazuje baner „Dostępna aktualizacja” i na „Aktualizuj” podmienia w tle węzły, bramkę i siebie (albo robi to sam, jeśli włączysz automat) |

## Szybki start

Pełny tutorial krok po kroku (DNS, firewall, pierwsza aplikacja, alerty): [docs/tutorial.pl.md](docs/tutorial.pl.md).

Wymagania: Linux z Dockerem i pluginem compose. Gotowe obrazy (amd64 + arm64) są w GHCR — nic nie budujesz.
Który plik z `deploy/` do czego służy, opisuje [deploy/README.md](deploy/README.md).

### 1. VPS — bramka i panel

```bash
git clone https://github.com/PanS4ge/deployra.git && cd deployra
DEPLOYRA_PUBLIC_HOST=vps.example.com docker compose -f deploy/vps.compose.yml up -d
docker logs deployra-wg | grep "Kod synchronizacji"   # z DEPLOYRA_LANG=en: "Sync code"
```

Otwórz panel (np. przez `ssh -L 8080:127.0.0.1:8080 vps`), przejdź konfigurację, a potem w **Tunel** wpisz adres `127.0.0.1:51821`, kod
i zaznacz „Panel działa na tym samym serwerze co bramka”. Panel wystawisz publicznie, dodając host proxy `deploy.example.com → 127.0.0.1:8080`.

Porty na firewallu VPS: `80/tcp`, `443/tcp`, `443/udp` (HTTP/3), `51820/udp`, `51821/tcp` (bootstrap — wystarczy na czas dołączania węzłów).

Panel może też stać gdzie indziej (`deploy/panel.compose.yml`): wtedy sam zestawi tunel do bramki. Panel razem z aplikacjami na jednej
maszynie (np. w domu), a na VPS-ie sama bramka: `deploy/panel-node.compose.yml` — panel sparuje węzeł obok siebie automatycznie.

### 2. Węzeł (np. domowy serwer)

W panelu: **Węzły → Dodaj węzeł → Przez tunel** — dostaniesz token i gotowy `node.compose.yml` (wystarczy sam ten plik,
bez klonowania repo). Albo z repozytorium:

```bash
DEPLOYRA_GATEWAY=vps.example.com:51821 DEPLOYRA_JOIN=dplr1.… docker compose -f deploy/node.compose.yml up -d
```

Węzeł generuje własny klucz WireGuard (prywatny nie opuszcza maszyny), dołącza do bramki, a panel sam dokończy parowanie.

### 3. Proxmox VE

- z Dockerem na hoście: `docker compose -f deploy/proxmox.compose.yml up -d`
- bez Dockera na hoście: `sudo bash deploy/install-native.sh` (usługa systemd, sam klient Dockera do stacków)

Backend znajdzie działające CT (`/proc/*/cgroup`), ich gniazda Dockera (`/proc/<pid>/root/run/docker.sock`) i nazwy (`/etc/pve/lxc/*.conf`).
W panelu na stronie węzła pojawi się przełącznik **Host / CT 101 · nextcloud / …**. Aplikacje wdrożone do CT są osiągalne pod IP kontenera —
dodaj jego podsieć w „Podsieciach LAN” węzła, żeby bramka do niego trafiła.

### 4. Węzeł za zewnętrznym WireGuardem (wg-easy)

Gdy serwer ma już własny WireGuard, **Węzły → Dodaj węzeł → Zewnętrzny WireGuard**: wpisz adres węzła w tej sieci (np. `10.8.0.1`),
podsieć i — jeśli węzeł ma dzielić sieć z kontenerem WireGuarda — jego nazwę (`wg-easy`). Panel otworzy stronę z gotowym compose
(`network_mode: "container:wg-easy"` albo sieć hosta), sprawdzi, czy widzi API węzła i czy bramka ma tunel z trasą do tej sieci
(**Tunel → Zewnętrzny WireGuard**: klient z wg-easy dla bramki), a na końcu sparuje węzeł kodem z `docker logs deployra-backend`.
Klientów w wg-easy dodajesz sam.

## Aktualizacje

Panel co 6 godzin sprawdza wydania w `PanS4ge/deployra` (Ustawienia → Aktualizacje, tam też zmienisz źródło). Gdy jest nowa wersja,
pokazuje baner z wyborem **Aktualizuj / Później / Pomiń tę wersję**. „Aktualizuj” uruchamia zadanie w tle: najpierw węzły (równolegle),
potem bramka, na końcu panel, a każdy krok czeka, aż komponent wstanie w nowej wersji. Jeśli coś się nie uda, panel zostaje w starej wersji.

Węzeł podmienia się sam: backend uruchamia na chwilę swoją kopię, która pobiera nowy obraz, podmienia kontener, a gdy nowa wersja
nie wstanie — przywraca starą, po czym znika. Panel i bramka nie mają `docker.sock`, więc od 1.3.0 ich kontener podmienia **węzeł
Deployry z tej samej maszyny** (panel wysyła żądanie, węzeł stawia kontener na nowej wersji, sprawdza healthcheck i w razie problemu
wraca do poprzedniej). Węzeł starszy niż 1.3.0 jest najpierw aktualizowany sam. Bramka na maszynie bez węzła i starsze instalacje
korzystają jeszcze z kontenera `*-updater`; bez żadnego z nich zostaje „Pull and redeploy”. W tabeli komponentów widać, czym
zaktualizuje się dany komponent, a przy „brak” — dlaczego. Tryby:

- **obrazy z GHCR** (domyślnie): `docker compose pull` + `up -d`;
- **ze źródeł** (`DEPLOYRA_IMAGES=deployra` w `deploy/.env`): `git` przewija repozytorium do taga `vX.Y.Z`, potem `build` + `up -d`;
- **Proxmox bez Dockera**: `install-native.sh --update` przez `systemd-run`.

### Wydawanie nowej wersji

1. Podbij `__version__` w `shared/`, `backend/`, `gateway/` i `frontend/` (`*/__init__.py`).
2. `git tag v1.1.0 && git push origin main v1.1.0`.

GitHub Actions ([.github/workflows/build.yml](.github/workflows/build.yml)) puszcza testy, buduje cztery obrazy dla `amd64` i `arm64`,
wypycha je do `ghcr.io/pans4ge/deployra-*` (`1.1.0`, `1.1`, `latest`) i tworzy wydanie z notatkami. Zwykły push uruchamia tylko testy;
obrazy `:edge` z `main` zbudujesz ręcznie (Actions → Build → Run workflow).

Gdy repozytorium jest prywatne, panel nie zobaczy wydań przez API GitHuba — wtedy sprawdza tagi publicznego obrazu
`ghcr.io/pans4ge/deployra-panel`. Paczki w GHCR muszą być publiczne, żeby działał zwykły `docker pull` bez logowania.

## Języki

Panel mówi po polsku i angielsku. Każdy użytkownik wybiera język w menu konta (zapisuje się przy koncie), domyślny ustawisz
w Ustawieniach → Ogólne albo zmienną `DEPLOYRA_LANG`; bez tego panel bierze język z przeglądarki. Bramka i węzły dostają język
użytkownika z każdym żądaniem, więc błędy i logi wdrożeń też przychodzą po angielsku. `DEPLOYRA_LANG` (pl / en) ustawia też
język logów kontenerów i stron dla odwiedzających („nieznany host”, zaślepka nowej strony).

W kodzie i szablonach są tylko klucze (`t("nodes.add")`), teksty leżą w paczkach:

- `frontend/deployra_panel/locales/<język>.json` — panel,
- `shared/deployra_common/locales/<język>.json` — bramka, węzeł i wspólny kod (walidacja, parowanie).

Nowy język: skopiuj oba `en.json` jako np. `de.json` i przetłumacz wartości — panel sam doda go do przełącznika. Język może też
przyjść jako [plugin](docs/plugins.pl.md#języki). Brakujące klucze spadają na angielski, a potem na polski. Test `frontend/tests/test_i18n.py` pilnuje, żeby paczki miały te same klucze i parametry, a każda strona
wyrenderowana po angielsku nie zawierała polskich liter.

## Ruch i bezpieczeństwo

Bramka liczy zapytania per adres IP z tych samych logów nginx co statystyki odwiedzin. Strona **Ruch i bezpieczeństwo** pokazuje:

- **topkę adresów IP** (24 h / 7 dni, per domena) z krajem i operatorem — z darmowych baz DB-IP Lite, które bramka pobiera co miesiąc
  (`DEPLOYRA_GEOIP=off` wyłącza);
- **podejrzane adresy** z ostatnich 5 minut: zalew zapytań (ponad próg na minutę), skanery (≥ 80% błędów 4xx) i szukanie dziur
  (`/.env`, `/wp-login.php`, `/.git/` …);
- **bany** na adres albo podsieć — na 1 h, dzień, 7 dni albo na stałe. nginx pokazuje stronę „Dostęp zablokowany” (403), co działa też dla ruchu przez
  Cloudflare; gdy jest `ipset`, bramka dodatkowo odrzuca pakiety w firewallu (`DEPLOYRA_BAN_FIREWALL=off` wyłącza);
- **auto-ban** (domyślnie wyłączony) dla adresów ponad progiem, z białą listą adresów, których nigdy nie banujemy.

Surowe adresy IP trzymamy 7 dni (`DEPLOYRA_IP_RETENTION_DAYS`) — adres IP to dana osobowa.

## Logowanie Deployra przed aplikacją

Lista dostępu z opcją **Wymagaj logowania Deployra** chroni dowolną aplikację kontem z panelu (forward auth przez nginx
`auth_request`), także aplikację bez własnego logowania. Można zawęzić dostęp do wybranych użytkowników. Po zalogowaniu panel
przekierowuje z powrotem jednorazowym tokenem, a aplikacja dostaje własne ciasteczko (działa też na domenach innych niż panel)
i nagłówek `X-Deployra-User` z e-mailem. Wymaga HTTPS na hoście i ustawionej domeny panelu; z hasłami Basic Auth łączy się tylko
w trybie „wystarczy jeden warunek”.

## HTTP/3

Host proxy z certyfikatem może mieć włączone **HTTP/3** (QUIC): bramka dodaje `listen 443 quic` i nagłówek `Alt-Svc`, o ile jej
nginx ma moduł `http_v3` (oficjalne obrazy 1.25+). Na firewallu trzeba otworzyć `443/udp`; gdy UDP nie przechodzi, przeglądarka
zostaje przy HTTP/2.

## Logi

**Logi** przeszukują logi kontenerów na wszystkich węzłach naraz — bez Elasticsearcha i bez dodatkowego RAM-u (czytane z Dockera
na żądanie). Składnia: `błąd timeout` (wszystkie słowa), `"fraza"`, `/regex/`, `-słowo`, `app:`, `container:`, `node:`, `level:error|warn`;
zakres 15 min – 7 dni, wykres trafień w czasie, kliknięcie kontenera zawęża wyniki.

## Mapa

**Mapa** rysuje infrastrukturę jako graf (force-graph): panel, bramki, węzły, kontenery LXC, aplikacje i domeny, z kolorem stanu
na obwódce i przepływem ruchu na połączeniach. Kliknięcie otwiera dany element; układ swobodny, warstwowy albo promienisty,
typy elementów można ukrywać.

## Aplikacje z plików

**Aplikacje → Nowa → Z plików** pokazuje gotowe środowiska. Wybierasz jedno, wgrywasz ZIP (albo nie — dostaniesz pliki startowe),
wskazujesz węzeł i domenę. Pliki lądują na węźle w `/srv/deployra/sites/<slug>` (dostępne też przez SFTP), jednorazowy kontener
instaluje zależności (`pip`, `npm`, `bundle`, `go build`, `cargo`…), a aplikacja startuje z oficjalnego obrazu języka z podmontowanymi plikami.

| Środowisko | Start | Port |
|---|---|---|
| PHP + Apache | `index.php`, `.htaccess` (mod_rewrite, mysqli, pdo_mysql) | 80 |
| Python / Flask / FastAPI / Django | `python main.py` / `gunicorn app:app` / `uvicorn main:app` / `gunicorn project.wsgi` | — / 8000 |
| Node.js / Express / Next, Nuxt, Astro | `node index.js` / `npm start` / `npm run build` + `npm start` | — / 3000 |
| Bun · Deno · Go · Rust | `bun run index.ts` · `deno run main.ts` · `go build` · `cargo build` | 3000 · 8000 · 8080 · 8080 |
| Ruby · Java · .NET · shell | `rackup` · `java -jar *.jar` · `dotnet App.dll` · `sh run.sh` | 9292 · 8080 · 8080 · — |

Bez portu = proces w tle (bot, worker, skrypt), bez domeny. Polecenie startowe i instalacji można nadpisać.
`data/` (trwałe dane, np. SQLite) i `.deployra/` (pamięć podręczna) zostają przy wgraniu nowego ZIP-a; nowy ZIP wdraża aplikację
ponownie (PHP widzi zmiany od razu).

## Wdrożenia bez przestojów

Opcja **Bez przestojów** przy aplikacji (obraz, Git, z plików — z opublikowanym portem): węzeł stawia nowy kontener obok starego
na wolnym porcie, czeka, aż wstanie i — jeśli podasz **ścieżkę sprawdzania zdrowia** (np. `/up`) — aż odpowie kodem poniżej 500.
Dopiero wtedy panel przełącza bramkę na nowy port, a stary kontener znika po 10 s (dokończenie trwających połączeń). Gdy nowa wersja
nie wstanie, stara działa dalej bez przerwy. Domyślnie wyłączone: przez chwilę oba kontenery montują te same wolumeny.

## Zmienne środowiskowe

Każda zmienna aplikacji ma zakres: **działanie** (domyślnie), **budowanie** (argumenty `docker build` — Dockerfile musi mieć `ARG`)
albo **oba**. Zmienne można edytować jak formularz albo jako tekst `.env` (cudzysłowy, wartości wieloliniowe, komentarze).
**Ustawienia → Zmienne współdzielone** to nazwane, zaszyfrowane zestawy zmiennych dołączane do wielu aplikacji; własne zmienne
aplikacji wygrywają z zestawem, a zmiana zestawu wchodzi przy następnym wdrożeniu.

## Zadania cykliczne

Karta **Zadania** na stronie aplikacji: polecenie wykonywane w działającym kontenerze (`sh -c`) według harmonogramu cron
(`*/15 * * * *`, `@daily`…), z limitem czasu, historią ostatnich 50 uruchomień (kod wyjścia, czas, wynik) i przyciskiem
„Uruchom teraz”. Tworzenie i uruchamianie zadań wymaga tego samego uprawnienia co terminal kontenera.

## Kopie baz danych

**Ustawienia → Kopia zapasowa → Kopie baz danych**: harmonogram zrzutów kontenera bazy (`pg_dump`, `mysqldump`, `mongodump`)
do tego samego magazynu co kopia panelu (S3/R2/MinIO, SFTP, FTP, dysk), z liczbą trzymanych kopii. Przy każdym harmonogramie widać
ostatni wynik i listę plików do pobrania albo **przywrócenia** do kontenera; nieudany zrzut wysyła alert.

## Zasoby i alerty

Panel co minutę zbiera CPU, RAM i dysk każdego węzła oraz CPU/RAM każdego kontenera aplikacji: 48 godzin co minutę,
30 dni jako średnie i maksima godzinowe. Wykresy są na stronie węzła (24 h / 7 dni / 30 dni) i aplikacji.

**Ustawienia → Alerty**:

- **kanały**: panel, webhook (JSON, opcjonalny podpis HMAC w `X-Deployra-Signature`), Discord, Slack, Telegram, ntfy, Gotify,
  Pushover, e-mail (SMTP); każdy kanał ma przycisk **Test**;
- **reguły**: CPU / RAM / dysk węzła ponad progiem przez N minut, węzeł nie odpowiada, RAM aplikacji blisko limitu, aplikacja zabita
  za brak pamięci (OOM), restarty w kółko, monitor uptime padł, nieudane wdrożenie, przeniesiona aplikacja, nowa wersja — każda reguła
  ma własny próg, czas i kanały.

Ochrona przed przepełnieniem: pierwsze wdrożenie sprawdza, czy węzeł ma dość wolnego RAM-u (limit aplikacji albo 64 MB) i dysku,
i podpowiada węzeł z większym zapasem. Kreator pokazuje wolny RAM przy każdym węźle, a środowiska mają domyślny limit pamięci.

## Minecraft

Sekcja **Minecraft** zarządza serwerami z pluginem **DeployraBridge** (Paper/Spigot/Purpur 1.8.8–1.21): konsola z makrami, gracze
(tryb gry, leczenie, OP, whitelist, kick, ban), `server.properties`, pliki i PlaceholderAPI.

- **Serwer na węźle**: Aplikacje → Nowa → **Minecraft (Paper)** (`itzg/minecraft-server`, port gry 25565). Plugin pobiera się z panelu
  i paruje sam.
- **Hosting zewnętrzny** (Craftserve, Pterodactyl…): Minecraft → Dodaj serwer zewnętrzny, wgraj plugin, w konsoli
  `deployra pair <adres-panelu> <kod>`. Opcjonalne FTP hostingu służy do plików, gdy serwer jest wyłączony.

Serwer nie otwiera żadnego portu: plugin sam łączy się z panelem (długie zapytanie po HTTPS), a żądania i odpowiedzi są podpisane
HMAC z ochroną przed powtórzeniem. Szczegóły: `minecraft-plugin/README.md`.

## Przenoszenie aplikacji między węzłami

**Aplikacja → Przenieś na inny serwer**: stary węzeł zatrzymuje aplikację i pakuje pliki, wolumeny Dockera, stack compose i konta SFTP,
panel przenosi paczkę na nowy węzeł, który ją rozpakowuje, a potem idzie zwykłe wdrożenie — host proxy dostaje nowy adres; domena,
DNS i certyfikat zostają. Gdy coś się nie uda przed przełączeniem, aplikacja wraca do pracy na starym węźle. Stara kopia zostaje,
chyba że zaznaczysz „usuń dane”. Katalogi hosta montowane w aplikacji (np. `/mnt/media`) nie są przenoszone — log je wypisuje.

Automatyczne przenosiny: włączasz je przy aplikacji (tylko bez wolumenów i bez compose) i w regule **Węzeł: RAM**. Gdy węzeł trzyma się
ponad progiem, największa taka aplikacja jedzie na węzeł z największym zapasem pamięci (najwyżej jedna na godzinę z danego węzła).

## Przejęcie kontenerów spoza Deployry

**Węzeł → Kontenery → Przejmij jako aplikację** (albo przycisk na stronie kontenera). Deployra czyta `docker inspect`: obraz, polecenie,
port, zmienne (bez domyślnych z obrazu), wolumeny i limity. Stary kontener staje i zostaje jako kopia `<nazwa>-pre-deployra`, a na jego
miejscu powstaje aplikacja z tym samym obrazem, danymi i portem hosta — od teraz z wdrożeniami, domeną, alertami i przenoszeniem.
Jeśli nowy kontener nie wstanie, stary wraca sam. Kontenery stacków compose przejmuje się jako stack (Węzeł → Stacki).

## Konta z przypisanymi serwerami (jak w Pterodactylu)

**Ustawienia → Użytkownicy → edycja → Dostęp: Tylko przypisane serwery.** Takie konto widzi w panelu tylko **Moje serwery** —
każdy inny adres jest zamknięty (nie tylko ukryty w menu). Przy użytkowniku (ikona serwerów) przypisujesz aplikacje albo kontenery
i dla każdego zaznaczasz uprawnienia: **zasilanie** (start / stop / restart / kill), **logi**, **konsola**, **pliki** (wgrywanie ZIP),
**wdrażanie** i **zmienne** (edycja env aplikacji). Użytkownik widzi zużycie RAM/CPU, status i adres swoich serwerów.

## Dwie bramki („widelec”)

Ruch publiczny może wchodzić inną maszyną niż hub WireGuard — np. Oracle (80/443) + mikr.us (WireGuard). Bramka brzegowa dołącza do
huba jak węzeł (**Tunel → Bramka brzegowa**), a panel przenosi na nią hosty proxy, certyfikaty, statystyki i bany. Instrukcja krok po
kroku: [docs/tutorial.pl.md](docs/tutorial.pl.md).

## Pluginy

**Ustawienia → Pluginy** pokazują listę pluginów, włączają je i wyłączają oraz instalują nowe z ZIP. Plugin to katalog z
`plugin.json` w `<DEPLOYRA_DATA>/plugins/` i może dodać:

- szablony aplikacji do kreatora — wbudowany *Katalog aplikacji* ma Uptime Kumę, Vaultwardena, Giteę, n8n, Grafanę, File Browser
  i WordPressa z MariaDB, a sekrety `${RANDOM}` losują się na miejscu;
- paczki językowe, także nowe języki;
- kod Pythona: strony z pozycją w menu, obsługę zdarzeń (wdrożenie, awaria, monitor w dół, nowa wersja, każde powiadomienie…)
  i własne ustawienia.

Wgrane pluginy startują wyłączone, a plugin z kodem może włączyć tylko ktoś z uprawnieniem do ryzykownych ustawień — kod pluginu
działa z uprawnieniami panelu. Dokumentacja i przykład: [docs/plugins.pl.md](docs/plugins.pl.md),
[examples/plugins/events-log](examples/plugins/events-log) (dziennik zdarzeń z webhookiem).

## Przesiadka z Portainera i NPM

1. Postaw bramkę i węzeł (wyżej). Istniejące kontenery od razu widać w zakładce **Kontenery**.
2. **Import z NPM**: wgraj `database.sqlite` (`docker cp npm:/data/database.sqlite .`), wskaż, że stary adres (np. `10.8.0.2`
   z drugiego NPM-a) to teraz węzeł „dom”, i zaimportuj.
3. Przestaw DNS domen na IP bramki (albo zaznacz rekordy DNS / Cloudflare Tunnel). Certyfikaty wystawią się po imporcie.
4. Gdy wszystko działa — wyłącz NPM-y i Portainera. 🎉

## Bezpieczeństwo

- **Synchronizacja** (dawne „Diffie-Hellman”): X25519 + HKDF, wiadomości pieczętowane HMAC-iem z jednorazowego kodu / tokenu —
  nie da się podmienić kluczy bez kodu. Stare DH na liczbach pierwszych < 100 z XOR-em jest usunięte.
- Każde żądanie panel → bramka/węzeł jest podpisane HMAC-SHA256 (znacznik czasu ±60 s, nonce przeciw powtórkom); ruch idzie tunelem WG.
- API węzła i bramki słucha tylko na adresie w tunelu; pula portów aplikacji jest dostępna wyłącznie z `wg0` (reguła `DOCKER-USER`).
- Hasła: scrypt z solą. Sesje: ciasteczko httponly + token CSRF. Sekrety (tokeny Cloudflare, klucze) zaszyfrowane AES-GCM w bazie.
- Terminal, surowy nginx, tunel i usuwanie są osobnymi (ryzykownymi) uprawnieniami.

## Zużycie RAM

Wszystko na `python:3.13-alpine`, jeden worker uvicorn, ciężkie biblioteki ładowane leniwie (np. asyncssh dopiero przy pierwszym koncie SFTP).
Limity w plikach compose: panel 160 MB, backend 192 MB, Deployra-WG 128 MB, nginx 96 MB. Zmierzone (RSS) w środowisku
deweloperskim na Windowsie (CPython 3.14, z narzutem DLL): panel ~87 MB, backend ~87 MB (bez SFTP; najwięcej waży docker-py),
bramka ~72 MB. Na Linuksie/musl wychodzi zwykle mniej — sprawdź `docker stats` u siebie. Aktualizator to `sleep` w pętli
(~1 MB + kilka MB narzutu Dockera na kontener).

## Rozwój

```bash
python -m venv .venv && .venv/Scripts/pip install -e shared fastapi uvicorn jinja2 python-multipart httpx cryptography docker psutil asyncssh pymysql loguru pyyaml websockets pytest
python scripts/dev_stack.py --reset --seed   # bramka + węzeł (atrapa Dockera) + panel na localhoście, z przykładowymi danymi
```

Panel: http://127.0.0.1:8080 (`admin@deployra.local` / `kosmos-demo`). Działa na Windowsie — bez WireGuarda, nginx i Dockera.

Testy: `pytest shared/tests`, `cd backend && pytest tests`, `cd gateway && pytest tests`, `cd frontend && pytest tests`,
`bash updater/tests/test_updater.sh` (aktualizator z atrapą Dockera i prawdziwym gitem).

Struktura: `shared/` (podpisy, parowanie, WireGuard, zadania, schematy, aktualizacje) · `backend/` (węzeł) · `gateway/` (Deployra-WG) ·
`frontend/` (panel) · `updater/` (sidecar aktualizacji) · `deploy/` (compose) · `docs/research/` (źródła decyzji).
Stary kod (NiceGUI) został w historii gita.

## Znane ograniczenia

- WireGuard, nginx, lego, cloudflared i sam aktualizator nie były jeszcze uruchomione na prawdziwym serwerze — testy pokrywają logikę.
- Hosty za proxy Cloudflare (pomarańczowa chmurka, nie tunel) liczą odwiedzających po adresach krawędzi Cloudflare.
- Stacki w CT na Proxmoxie: pliki compose leżą na hoście, więc montowania ścieżek względnych nie zadziałają — używaj wolumenów nazwanych.
- Wdrożenia bez przestojów, logowanie Deployra, HTTP/3 i przywracanie baz są pokryte testami z atrapą Dockera i nginx — przy
  pierwszym użyciu patrz w log wdrożenia i `nginx -t` na bramce.
