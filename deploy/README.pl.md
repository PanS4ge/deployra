# deploy/ — który plik do czego

[English](README.md) · **Polski**

Każdy plik to osobny „zestaw” do uruchomienia przez `docker compose -f deploy/<plik> up -d`.
Wybierasz go według tego, **co stoi na danym serwerze**.

| Plik | Gdzie uruchomić | Co stawia |
|---|---|---|
| `vps.compose.yml` | VPS z publicznym IP — **najczęstszy wybór** | bramkę Deployra-WG + nginx + panel (dołącza dwa pliki niżej) |
| `gateway.compose.yml` | VPS z publicznym IP, gdy panel stoi gdzie indziej | tylko bramkę: hub WireGuard, nginx dla ruchu publicznego, certyfikaty |
| `panel.compose.yml` | dowolny serwer (np. w domu), gdy bramka jest na innym VPS | tylko panel Deployry |
| `node.compose.yml` | każdy serwer z Dockerem, na którym mają działać aplikacje (domowy serwer, drugi VPS) | Deployra Backend — węzeł |
| `proxmox.compose.yml` | host Proxmox VE z Dockerem | węzeł, który widzi Docker hosta **i** każdy CT z Dockerem |
| `install-native.sh` | host Proxmox VE **bez** Dockera | to samo co wyżej, jako usługa systemd |

Typowy układ: `vps.compose.yml` na VPS, `node.compose.yml` (albo `proxmox.compose.yml`) na każdym serwerze w domu.

```
 VPS:   vps.compose.yml  = gateway.compose.yml + panel.compose.yml
 Dom:   node.compose.yml  /  proxmox.compose.yml  /  install-native.sh
```

## Aktualizator (usługi `*-updater`)

Każdy plik ma mały kontener `deployra-*-updater`: w spoczynku ~1 MB RAM, bez sieci, tylko z `docker.sock`.
Panel pokazuje baner „Dostępna aktualizacja”. Po kliknięciu „Aktualizuj” prosi o nią po kolei węzły, bramkę i na końcu siebie,
a aktualizator każdego z nich pobiera nowe obrazy i podmienia kontenery. Panel i bramka nie mają dostępu do Dockera.

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
| `DEPLOYRA_VERSION` | `latest` | przypięcie wersji, np. `1.0.0` (aktualizator przestawi pin sam) |
| `DEPLOYRA_NAME`, `DEPLOYRA_GATEWAY`, `DEPLOYRA_JOIN` | — | dołączanie węzła; gotowe wartości daje panel (Węzły → Dodaj węzeł) |
| `DEPLOYRA_UPDATE_REPO` | `PanS4ge/deployra` | skąd panel sprawdza wydania |
| `DEPLOYRA_LANG` | `pl` (panel: z przeglądarki) | język logów, stron dla odwiedzających i domyślny język panelu (`pl` / `en`) |
| `DEPLOYRA_MAX_TRANSFER_MB` | `51200` | węzeł: największa paczka przyjmowana przy przenoszeniu aplikacji |
| `DEPLOYRA_PLUGINS_DIR` | `<DEPLOYRA_DATA>/plugins` | panel: katalog pluginów ([docs/plugins.pl.md](../docs/plugins.pl.md)) |
| `DEPLOYRA_GEOIP` | `on` | kraj i ISP adresów IP (bazy DB-IP Lite pobierane przez bramkę) |
| `DEPLOYRA_IP_RETENTION_DAYS` | `7` | ile dni bramka trzyma surowe adresy IP |
| `DEPLOYRA_BAN_FIREWALL` | `on` | bany także w ipset/iptables, nie tylko w nginx |
