# Deployra ✦

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
| **Aplikacje** | kreator: obraz Dockera, docker-compose, strona statyczna (ZIP + SFTP), repozytorium Git (build z Dockerfile, webhook GitHub/Gitea) · domena, rekord DNS w Cloudflare, Let's Encrypt i test przez tunel w jednym kroku · rollback |
| **Docker** (zamiast Portainera) | kontenery, logi i statystyki na żywo, terminal w przeglądarce, obrazy, wolumeny, sieci, stacki compose |
| **Proxmox** | backend na hoście Proxmoxa pokazuje **każdy kontener LXC z Dockerem** — bez instalowania agenta w każdym CT |
| **Proxy** (zamiast NPM) | hosty proxy, przekierowania, hosty 404, streamy TCP/UDP, listy dostępu, własne lokacje, surowy `advanced` nginx z automatycznym rollbackiem |
| **Import z NPM** | z pliku `database.sqlite` (razem z hasłami list dostępu) albo przez API; mapowanie starych adresów na węzły; certyfikaty przenoszone albo wystawiane na nowo |
| **Tunel** | hub WireGuard, dołączanie węzłów tokenem, routing podsieci LAN (NAS, Home Assistant…), węzły widzą się nawzajem |
| **Cloudflare** | rekordy A/AAAA per host (bez wildcarda), certyfikaty wildcard (DNS-01), **Cloudflare Tunnel** (cloudflared — bez publicznego IP i otwartych portów) |
| **Odwiedziny** | unikalni i powracający per domena, wykresy jak w Cloudflare — bez ciasteczek, bez botów, liczone na bramce z logów nginx |
| **Uptime** (à la Uptime Kuma) | monitory aplikacji, kontenerów, węzłów i URL, uptime 24 h / 7 dni / 30 dni, powiadomienia (panel, Discord, ntfy, Slack) |
| **Panel** | FastAPI + Jinja2 + htmx + Alpine (bez NiceGUI, bez kroku build), ciemny kosmiczny motyw, użytkownicy i uprawnienia, SQLite albo MySQL/MariaDB |
| **Aktualizacje** | panel sprawdza wydania, pokazuje baner „Dostępna aktualizacja” i na „Aktualizuj” podmienia w tle węzły, bramkę i siebie (albo robi to sam, jeśli włączysz automat) |

## Szybki start

Wymagania: Linux z Dockerem i pluginem compose. Gotowe obrazy (amd64 + arm64) są w GHCR — nic nie budujesz.
Który plik z `deploy/` do czego służy, opisuje [deploy/README.md](deploy/README.md).

### 1. VPS — bramka i panel

```bash
git clone https://github.com/PanS4ge/deployra.git && cd deployra
DEPLOYRA_PUBLIC_HOST=vps.example.com docker compose -f deploy/vps.compose.yml up -d
docker logs deployra-wg | grep "Kod synchronizacji"
```

Otwórz panel (np. przez `ssh -L 8080:127.0.0.1:8080 vps`), przejdź konfigurację, a potem w **Tunel** wpisz adres `127.0.0.1:51821`, kod
i zaznacz „Panel działa na tym samym serwerze co bramka”. Panel wystawisz publicznie, dodając host proxy `deploy.example.com → 127.0.0.1:8080`.

Porty na firewallu VPS: `80/tcp`, `443/tcp`, `51820/udp`, `51821/tcp` (bootstrap — wystarczy na czas dołączania węzłów).

Panel może też stać gdzie indziej (`deploy/panel.compose.yml`): wtedy sam zestawi tunel do bramki.

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

## Aktualizacje

Panel co 6 godzin sprawdza wydania w `PanS4ge/deployra` (Ustawienia → Aktualizacje, tam też zmienisz źródło). Gdy jest nowa wersja,
pokazuje baner z wyborem **Aktualizuj / Później / Pomiń tę wersję**. „Aktualizuj” uruchamia zadanie w tle: najpierw węzły (równolegle),
potem bramka, na końcu panel, a każdy krok czeka, aż komponent wstanie w nowej wersji. Jeśli coś się nie uda, panel zostaje w starej wersji.

Podmianą zajmuje się kontener `*-updater` obok każdego komponentu (w spoczynku ~1 MB RAM, bez sieci). Tylko on ma `docker.sock`,
panel i bramka go nie dostają. Działa w trzech trybach:

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
- Jedna bramka na panel (wiele bramek — w planach).
