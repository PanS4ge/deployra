# Jak postawić Deployrę — krok po kroku

Na końcu będziesz mieć: bramkę i panel na VPS-ie, (opcjonalnie) domowy serwer podpięty tunelem, pierwszą aplikację pod własną domeną
z HTTPS i alerty na Discordzie albo Telegramie.

```
 Internet ──► VPS: Deployra-WG (nginx, certyfikaty, WireGuard) + panel
                    │ tunel WireGuard
                    ▼
              Dom / drugi VPS: węzeł (Docker z aplikacjami)
```

## 0. Czego potrzebujesz

- **VPS z publicznym IP** — Linux (Debian/Ubuntu), od 512 MB RAM (1 GB wygodniej), Docker z pluginem compose.
- **Domena**, np. `example.com`. Najwygodniej z DNS w Cloudflare (Deployra sama doda rekordy), ale wystarczy dowolny DNS.
- (opcjonalnie) **serwer w domu** albo drugi VPS z Dockerem — tam mogą działać aplikacje. Aplikacje mogą też działać na samym VPS-ie (krok 6b).

## 1. DNS

U rejestratora / w Cloudflare dodaj rekordy `A` na IP VPS-a:

| Nazwa | Wartość | Po co |
|---|---|---|
| `deploy.example.com` | IP VPS | panel |
| `*.example.com` (opcjonalnie) | IP VPS | każda nowa aplikacja od razu ma `nazwa.example.com` |

Bez rekordu `*` każdą domenę aplikacji dodasz osobno — albo zrobi to Deployra, jeśli w kroku 5 podasz token Cloudflare.
W Cloudflare zostaw rekordy na razie **bez pomarańczowej chmurki** (DNS only), dopóki nie wystawisz certyfikatów.

## 2. Docker i firewall na VPS-ie

```bash
curl -fsSL https://get.docker.com | sh
```

Otwórz porty (przykład dla `ufw`):

```bash
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw allow 51820/udp && ufw allow 51821/tcp && ufw enable
```

`51820/udp` to WireGuard, `51821/tcp` to dołączanie węzłów (po dołączeniu wszystkich możesz go zamknąć).
U niektórych dostawców (Oracle, Hetzner Cloud, AWS) trzeba to samo otworzyć też w panelu dostawcy.

## 3. Bramka i panel na VPS-ie

```bash
git clone https://github.com/PanS4ge/deployra.git && cd deployra
```

Dopóki repozytorium jest prywatne, `git clone` zapyta o login — jako hasło podaj token z GitHuba (Settings → Developer settings →
Personal access tokens, uprawnienie `repo`).

**Wariant A — gotowe obrazy z GHCR** (wymaga wydania z taga `vX.Y.Z` i publicznych paczek w GHCR):

```bash
DEPLOYRA_PUBLIC_HOST=vps.example.com docker compose -f deploy/vps.compose.yml up -d
```

**Wariant B — budowanie z repozytorium** (działa od razu, z najnowszym kodem z `main`; build trwa kilka minut):

```bash
printf 'DEPLOYRA_PUBLIC_HOST=vps.example.com\nDEPLOYRA_IMAGES=deployra\n' > deploy/.env
docker compose -f deploy/vps.compose.yml up -d --build
```

W miejsce `vps.example.com` wpisz publiczny adres albo IP VPS-a. Sprawdź, czy wszystko wstało, i wyciągnij kod synchronizacji bramki:

```bash
docker ps --format '{{.Names}}\t{{.Status}}'
docker logs deployra-wg 2>&1 | grep "Kod synchronizacji"
```

## 4. Pierwsze logowanie

Panel słucha tylko na `127.0.0.1:8080`, więc na początek wejdź przez tunel SSH — na **swoim komputerze**:

```bash
ssh -L 8080:127.0.0.1:8080 root@vps.example.com
```

Otwórz <http://localhost:8080>. Kreator konfiguracji poprosi o konto administratora (e-mail i hasło — to są dane logowania do panelu),
domenę bazową aplikacji (`example.com`) i domenę panelu (`deploy.example.com`).

Potem **Tunel → Połącz z Deployra-WG**:

- adres: `127.0.0.1:51821`,
- kod: ten z `docker logs deployra-wg`,
- zaznacz **„Panel działa na tym samym serwerze co bramka”**.

## 5. Panel pod własną domeną z HTTPS

1. **Ustawienia → Ogólne**: e-mail do Let's Encrypt.
2. (opcjonalnie) **Ustawienia → DNS i Cloudflare**: token API Cloudflare z uprawnieniem *Zone → DNS → Edit* — od tej pory Deployra
   sama dodaje rekordy dla nowych domen.
3. **Proxy → Nowy host**: domena `deploy.example.com`, cel `127.0.0.1`, port `8080`, włącz HTTPS i wymuszanie HTTPS.

Od teraz panel jest pod `https://deploy.example.com`, a tunel SSH nie jest już potrzebny.

## 6. Serwer na aplikacje

### 6a. Domowy serwer albo drugi VPS (przez tunel)

1. Na serwerze zainstaluj Dockera (`curl -fsSL https://get.docker.com | sh`).
2. W panelu: **Węzły → Dodaj węzeł → Przez tunel Deployra-WG**. Panel pokaże gotowe polecenie z `DEPLOYRA_GATEWAY` i jednorazowym
   tokenem `DEPLOYRA_JOIN`, np.:

   ```bash
   DEPLOYRA_GATEWAY=vps.example.com:51821 DEPLOYRA_JOIN=dplr1.… docker compose -f deploy/node.compose.yml up -d
   ```

   (wystarczy sam plik `deploy/node.compose.yml` — panel daje go do pobrania).
3. Po kilkunastu sekundach węzeł pojawi się w panelu jako **sparowany**. Serwer w domu nie potrzebuje publicznego IP ani otwartych portów.

Proxmox: zamiast `node.compose.yml` użyj `deploy/proxmox.compose.yml` (albo `sudo bash deploy/install-native.sh`, gdy na hoście nie ma Dockera).

### 6b. Aplikacje na samym VPS-ie

Na VPS-ie (w katalogu `deployra`):

```bash
DEPLOYRA_NAME=vps DEPLOYRA_API_BIND=127.0.0.1 docker compose -f deploy/node.compose.yml up -d
docker logs deployra-backend 2>&1 | grep "Kod parowania"
```

W panelu: **Węzły → Dodaj węzeł → Bezpośrednio**: adres `http://127.0.0.1:2750`, kod z logów, adres dla proxy `127.0.0.1`.

## Wariant: panel i aplikacje w domu, na VPS-ie tylko bramka

Zamiast kroków 3–6 możesz postawić na VPS-ie samą bramkę, a panel z węzłem na jednej maszynie w domu.
VPS potrzebuje wtedy tylko ~250 MB RAM, a panel, aplikacje i ich dane są u ciebie.

1. **VPS**: Docker, firewall i repozytorium jak w krokach 2–3, a potem:

   ```bash
   docker compose -f deploy/gateway.compose.yml up -d --build
   ```
   ```bash
   docker logs deployra-wg 2>&1 | grep "Kod synchronizacji"
   ```

2. **Domowy serwer**: Docker i repozytorium (z `DEPLOYRA_IMAGES=deployra` w `deploy/.env`), potem:

   ```bash
   DEPLOYRA_NAME=dom docker compose -f deploy/panel-node.compose.yml up -d --build
   ```

3. Wejdź na `http://<adres-domowego-serwera>:8080`, załóż konto administratora, a w **Tunel → Połącz z Deployra-WG** wpisz
   `vps.example.com:51821` i kod z kroku 1 (bez zaznaczania „ten sam serwer”). Węzeł **dom** pojawi się w **Węzłach** sam — panel
   paruje go automatycznie.
4. Panel pod domeną: **Proxy → Nowy host** `deploy.example.com` → węzeł **dom**, port `8080`, HTTPS.

Ruch z internetu idzie: domena → bramka na VPS-ie → tunel WireGuard → aplikacje w domu. Domowy serwer nie potrzebuje publicznego IP
ani otwartych portów. Porty aplikacji są osiągalne tylko przez tunel (i z samej maszyny), SFTP (`2222`) — z sieci lokalnej.

## Wariant: dwie bramki — ruch publiczny na jednym VPS-ie, WireGuard na drugim („widelec”)

Np. **Oracle** ma publiczne 80/443 i aplikacje w chmurze, a **mikr.us** trzyma WireGuarda, do którego dołącza dom.
Ruch idzie: internet → bramka brzegowa (Oracle) → tunel przez hub (mikr.us) → dom. To jeden przeskok sieciowy przez hub —
bez drugiego nginx po drodze, a certyfikaty, bany i statystyki są na Oracle.

```
 Internet ─► Oracle: bramka brzegowa ═╗
                                       ║ WireGuard
             mikr.us: hub ◄════════════╝══════► dom: węzeł (albo panel + węzeł)
```

1. **mikr.us — hub.** W panelu mikr.us sprawdź przydzielone porty (np. `20123` i `20124`) i użyj ich zamiast 51820/51821:

   ```bash
   printf 'DEPLOYRA_PUBLIC_HOST=srv12.mikr.us\nDEPLOYRA_WG_PORT=20124\nDEPLOYRA_BOOTSTRAP_PORT=20123\nDEPLOYRA_IMAGES=deployra\n' > deploy/.env
   ```
   ```bash
   docker compose -f deploy/gateway.compose.yml up -d --build
   ```

2. **Panel** (w domu z `panel-node.compose.yml` albo gdziekolwiek) — **Tunel → Połącz z Deployra-WG**: `srv12.mikr.us:20123` + kod
   z `docker logs deployra-wg` na mikr.us.
3. **Oracle — bramka brzegowa.** W panelu: **Tunel → Bramka brzegowa** → wpisz publiczny adres Oracle → **Dodaj**. Panel pokaże
   polecenie z `DEPLOYRA_HUB` i jednorazowym tokenem `DEPLOYRA_JOIN` — uruchom je na Oracle w katalogu z repozytorium
   (z `DEPLOYRA_IMAGES=deployra` w `deploy/.env` dopisz `--build`). Na Oracle otwórz `80/tcp`, `443/tcp` i `443/udp` (HTTP/3); WireGuard UDP nie jest potrzebne,
   bo to Oracle łączy się z mikr.us.
4. Po minucie odśwież stronę **Tunel**: panel sam sparuje się z bramką brzegową, przeniesie na nią hosty proxy i certyfikaty,
   a hub dostanie pusty stan. Rekordy DNS domen kieruj na **Oracle** (z tokenem Cloudflare panel robi to sam).

Aplikacje w chmurze na Oracle: postaw tam węzeł przez tunel (**Węzły → Dodaj węzeł → Przez tunel**, polecenie z panelu
z dopiskiem `DEPLOYRA_WG_IFACE=wg1`, bo `wg0` zajmuje bramka) albo podepnij gotowe usługi hostem proxy na **Dowolny adres** `127.0.0.1:port`.
Hosty proxy z adresem `127.0.0.1` po włączeniu bramki brzegowej wskazują na maszynę Oracle, nie na hub.

## 7. Pierwsza aplikacja

**Aplikacje → Nowa aplikacja**:

1. **Źródło** — gotowe kafelki:
   - *Z plików*: PHP + Apache, Python (skrypt / Flask / FastAPI / Django), Node.js (skrypt / Express / Next, Nuxt, Astro), Bun, Deno,
     Go, Rust, Ruby, Java (.jar), .NET, shell;
   - *Z szablonu*: Uptime Kuma, Vaultwarden, Gitea, n8n, Grafana, File Browser, WordPress;
   - albo obraz Dockera, docker-compose, strona statyczna, repozytorium Git.
2. **Gdzie** — nazwa i węzeł (obok każdego węzła widać wolny RAM).
3. **Szczegóły** — przy „Z plików” wgraj ZIP z kodem (bez ZIP-a dostaniesz działający przykład), sprawdź port i limit RAM.
4. **Domena** — np. `sklep.example.com`, HTTPS, rekord w Cloudflare.

**Wdróż** — log na żywo pokaże pobieranie obrazu, instalację zależności, start kontenera, host proxy, certyfikat i test HTTP.
Pliki zmienisz później: nowy ZIP na stronie aplikacji (wdroży się sama) albo przez SFTP (konta w **Węzły → węzeł → SFTP**).
Trwałe dane trzymaj w katalogu `data/` — przeżywa wgranie nowej wersji.

## 8. Alerty

**Ustawienia → Alerty**:

1. Dodaj kanał: Discord (URL webhooka kanału), Telegram (token bota od @BotFather + chat ID), ntfy, Gotify, Pushover, e-mail (SMTP)
   albo własny webhook. Kliknij **Test**.
2. W **Regułach** ustaw progi (np. RAM węzła 90% przez 5 minut, dysk 90%) i zaznacz, do których kanałów ma trafiać każda reguła.
3. Chcesz, żeby aplikacje same uciekały z przeciążonego serwera? Włącz „przenoś…” w regule **Węzeł: RAM** i „Przenoś automatycznie”
   na stronie aplikacji (tylko aplikacje bez wolumenów). Ręcznie przeniesiesz dowolną aplikację: **Aplikacja → Przenieś na inny serwer**.

## 9. Aktualizacje

Gdy wyjdzie nowa wersja, panel pokaże baner **Dostępna aktualizacja** — „Aktualizuj” podmieni po kolei węzły, bramkę i panel.
Przy wariancie B (budowanie z repozytorium) aktualizator sam przewinie repozytorium do nowego taga i zbuduje obrazy.

## Gdy coś nie działa

| Objaw | Co sprawdzić |
|---|---|
| Panel nie odpowiada | `docker logs deployra-panel` · czy działa tunel SSH (`ssh -L 8080:127.0.0.1:8080 …`) |
| Bramka nie ma kodu synchronizacji | `docker logs deployra-wg` · czy podałeś `DEPLOYRA_PUBLIC_HOST` |
| Węzeł nie dołącza | port `51821/tcp` i `51820/udp` otwarte na VPS-ie (także w panelu dostawcy) · `docker logs deployra-backend` |
| Certyfikat się nie wystawia | rekord DNS domeny wskazuje na VPS · port 80 otwarty · w Cloudflare chmurka wyłączona na czas wystawiania |
| Aplikacja „z plików” nie startuje | log wdrożenia (instalacja zależności) · **Aplikacja → Logi** · czy port w kreatorze zgadza się z tym, na którym słucha kod |
| Brak wykresów zasobów | pierwsze punkty pojawiają się po kilku minutach; stary węzeł bez `/api/usage` — zaktualizuj go |
