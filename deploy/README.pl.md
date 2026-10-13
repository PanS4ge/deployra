# deploy/ — który plik do czego

[English](README.md) · **Polski**

Każdy plik to osobny „zestaw” do uruchomienia przez `docker compose -f deploy/<plik> up -d`.
Wybierasz go według tego, **co stoi na danym serwerze**.

| Plik | Gdzie uruchomić | Co stawia |
|---|---|---|
| `vps.compose.yml` | VPS z publicznym IP — **najczęstszy wybór** | bramkę Deployra-WG + nginx + panel (dołącza dwa pliki niżej) |
| `gateway.compose.yml` | VPS z publicznym IP, gdy panel stoi gdzie indziej | tylko bramkę: hub WireGuard, nginx dla ruchu publicznego, certyfikaty |
| `panel.compose.yml` | dowolny serwer (np. w domu), gdy bramka jest na innym VPS | tylko panel Deployry |
| `panel-node.compose.yml` | jeden serwer (np. w domu) na panel **i** aplikacje, gdy bramka stoi sama na VPS-ie | panel + węzeł; panel sam paruje węzeł |
| `node.compose.yml` | każdy serwer z Dockerem, na którym mają działać aplikacje (domowy serwer, drugi VPS) | Deployra Backend — węzeł |
| `proxmox.compose.yml` | host Proxmox VE z Dockerem | węzeł, który widzi Docker hosta **i** każdy CT z Dockerem |
| `install-native.sh` | host Proxmox VE **bez** Dockera | to samo co wyżej, jako usługa systemd |

Typowy układ: `vps.compose.yml` na VPS, `node.compose.yml` (albo `proxmox.compose.yml`) na każdym serwerze w domu.

```
 VPS:   vps.compose.yml  = gateway.compose.yml + panel.compose.yml
 Dom:   node.compose.yml  /  proxmox.compose.yml  /  install-native.sh
```

Dwie bramki („widelec”): ruch publiczny na jednym VPS-ie (np. Oracle), WireGuard na drugim (np. mikr.us). Bramka brzegowa to ten sam
`gateway.compose.yml` z `DEPLOYRA_HUB` i `DEPLOYRA_JOIN` (polecenie daje panel: **Tunel → Bramka brzegowa**):

```
 Oracle:   gateway.compose.yml + DEPLOYRA_HUB=srv12.mikr.us:20123 DEPLOYRA_JOIN=…   (80/443, certyfikaty, statystyki)
 mikr.us:  gateway.compose.yml + DEPLOYRA_WG_PORT / DEPLOYRA_BOOTSTRAP_PORT        (hub WireGuard)
 Dom:      panel-node.compose.yml  albo  node.compose.yml
```

Panel w domu obok aplikacji, bramka sama na VPS-ie:

```
 VPS:   gateway.compose.yml
 Dom:   panel-node.compose.yml  = panel.compose.yml + node.compose.yml (parowane automatycznie)
```

## Stacki do wklejenia (`deploy/stacks/`)

Samodzielne pliki bez `build:` i bez odwołań do innych plików — wklejasz do Portainera (**Stacks → Add stack → Web editor**)
albo uruchamiasz `docker compose -f <plik> up -d`. Zmieniasz tylko linie oznaczone `←`. Obrazy pobierają się z GHCR.

| Plik | Co stawia |
|---|---|
| `stacks/vps.yml` | bramka + panel na jednym VPS-ie |
| `stacks/gateway.yml` | sama bramka (też hub na mikr.us albo bramka brzegowa na Oracle) |
| `stacks/panel-node.yml` | panel + węzeł na jednej maszynie (panel sam paruje węzeł) |
| `stacks/node.yml` | sam węzeł (token z panelu) |

Bez kontenerów aktualizatora: węzeł aktualizuje się sam z panelu, panel i bramkę — **Pull and redeploy** w Portainerze
(albo `docker compose pull && docker compose up -d`).

## Aktualizator (usługi `*-updater`)

Panel pokazuje baner „Dostępna aktualizacja”. Po kliknięciu „Aktualizuj” prosi o nią po kolei węzły, bramkę i na końcu siebie.

- **Węzeł** nie potrzebuje aktualizatora: backend i tak ma `docker.sock`, więc uruchamia na chwilę swoją kopię
  (`deployra-backend-update`), która pobiera nowy obraz, podmienia kontener (te same zmienne, wolumeny, sieć i etykiety
  stacka), czeka, aż nowa wersja wstanie, a jeśli nie wstanie — przywraca starą. Potem kopia znika.
  Działa też dla stacków z Portainera. Wersja przypięta w `.env` (`DEPLOYRA_VERSION`) nie jest przestawiana — zmień ją ręcznie.
- **Panel i bramka** nie mają dostępu do Dockera, więc obok nich stoi mały kontener `deployra-*-updater`
  (w spoczynku ~1 MB RAM, bez sieci, tylko z `docker.sock`).

## Zewnętrzny WireGuard (wg-easy, router w domu)

Gdy maszyny domowe są już w sieci innego serwera WireGuard (np. `10.8.0.0/24` za wg-easy na osobnym VPS-ie), bramka może
dołączyć tam jak zwykły klient: **Sieć → Zewnętrzny WireGuard**, wklej plik `.conf` klienta (w wg-easy: nowy klient → pobierz).
Hosty proxy kierujesz wtedy prosto na adresy `10.8.0.x` — bez Deployry na tych maszynach.

- Przez tunel idą tylko podane podsieci (domyślnie ta z `Address`). `AllowedIPs = 0.0.0.0/0` z wg-easy jest pomijane —
  inaczej cały ruch bramki szedłby przez cudzy serwer. `DNS` i `PostUp/PostDown` z pliku też są pomijane.
- Konfiguracja leży w danych bramki, więc tunel wstaje sam po restarcie i po „Pull and redeploy”.
- Interfejs postawiony wcześniej ręcznie pod tą samą nazwą (np. `wg1`) bramka przejmuje.
- Podsieć nie może być jednocześnie przypisana do peera Deployry — usuń ją najpierw z peera (albo martwego peera w całości).

## Obrazy

Domyślnie gotowe obrazy z GHCR (`linux/amd64` i `linux/arm64`), więc nic nie trzeba budować:

```
ghcr.io/pans4ge/deployra-panel     ghcr.io/pans4ge/deployra-wg
ghcr.io/pans4ge/deployra-backend   ghcr.io/pans4ge/deployra-updater
```

Zmienne w `deploy/.env` (obok plików compose):

| Zmienna | Domyślnie | Po co |
|---|---|---|
| `DEPLOYRA_PUBLIC_HOST` | — (wymagana dla bramki) | publiczny adres/IP VPS |
| `DEPLOYRA_IMAGES` | `ghcr.io/pans4ge/deployra` | prefiks obrazów; `deployra` = budowanie z repo (`up -d --build`, aktualizacja przez git) |
| `DEPLOYRA_VERSION` | `latest` | przypięcie wersji, np. `1.0.0` (aktualizator panelu i bramki przestawi pin sam) |
| `DEPLOYRA_NAME`, `DEPLOYRA_GATEWAY`, `DEPLOYRA_JOIN` | — | dołączanie węzła; gotowe wartości daje panel (Węzły → Dodaj węzeł) |
| `DEPLOYRA_UPDATE_REPO` | `PanS4ge/deployra` | skąd panel sprawdza wydania |
| `DEPLOYRA_LANG` | `pl` (panel: z przeglądarki) | język logów, stron dla odwiedzających i domyślny język panelu (`pl` / `en`) |
| `DEPLOYRA_HUB`, `DEPLOYRA_JOIN` | — | bramka brzegowa: adres huba WireGuard i token z panelu (Tunel → Bramka brzegowa) |
| `DEPLOYRA_WG_PORT`, `DEPLOYRA_BOOTSTRAP_PORT` | `51820`, `51821` | porty bramki (np. przydzielone przez mikr.us) |
| `DEPLOYRA_MAX_TRANSFER_MB` | `51200` | węzeł: największa paczka przyjmowana przy przenoszeniu aplikacji |
| `DEPLOYRA_PLUGINS_DIR` | `<DEPLOYRA_DATA>/plugins` | panel: katalog pluginów ([docs/plugins.pl.md](../docs/plugins.pl.md)) |
| `DEPLOYRA_GEOIP` | `on` | kraj i ISP adresów IP (bazy DB-IP Lite pobierane przez bramkę) |
| `DEPLOYRA_IP_RETENTION_DAYS` | `7` | ile dni bramka trzyma surowe adresy IP |
| `DEPLOYRA_BAN_FIREWALL` | `on` | bany także w ipset/iptables, nie tylko w nginx |
