# deploy/ — which file is for what

**English** · [Polski](README.pl.md)

Each file is a separate set you start with `docker compose -f deploy/<file> up -d`.
Pick it based on **what runs on a given server**.

| File | Where to run it | What it starts |
|---|---|---|
| `vps.compose.yml` | a VPS with a public IP — **the most common choice** | gateway Deployra-WG + nginx + panel (includes the two files below) |
| `gateway.compose.yml` | a VPS with a public IP, when the panel lives elsewhere | just the gateway: WireGuard hub, nginx for public traffic, certificates |
| `panel.compose.yml` | any server (e.g. at home), when the gateway is on another VPS | just the Deployra panel |
| `panel-node.compose.yml` | one server (e.g. at home) for the panel **and** apps, with the gateway alone on a VPS | panel + node; the panel pairs the node by itself |
| `node.compose.yml` | every server with Docker that should run apps (home server, a second VPS) | Deployra Backend — a node |
| `proxmox.compose.yml` | a Proxmox VE host with Docker | a node that sees the host's Docker **and** every CT running Docker |
| `install-native.sh` | a Proxmox VE host **without** Docker | the same as above, as a systemd service |

Typical setup: `vps.compose.yml` on the VPS, `node.compose.yml` (or `proxmox.compose.yml`) on each server at home.

```
 VPS:    vps.compose.yml  = gateway.compose.yml + panel.compose.yml
 Home:   node.compose.yml  /  proxmox.compose.yml  /  install-native.sh
```

Panel at home next to the apps, gateway alone on the VPS:

```
 VPS:    gateway.compose.yml
 Home:   panel-node.compose.yml  = panel.compose.yml + node.compose.yml (paired automatically)
```

## Updater (`*-updater` services)

Every file includes a small `deployra-*-updater` container: ~1 MB RAM when idle, no network, only `docker.sock`.
The panel shows an “Update available” banner. After you click “Update” it asks the nodes, the gateway and finally itself to update,
and each component's updater pulls the new images and replaces the containers. The panel and the gateway get no access to Docker.

## Images

Ready-made images from GHCR by default (`linux/amd64` and `linux/arm64`), so there's nothing to build:

```
ghcr.io/pans4ge/deployra-panel     ghcr.io/pans4ge/deployra-wg
ghcr.io/pans4ge/deployra-backend   ghcr.io/pans4ge/deployra-updater
```

Variables in `deploy/.env` (next to the compose files):

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOYRA_PUBLIC_HOST` | — (required for the gateway) | the VPS's public address/IP |
| `DEPLOYRA_IMAGES` | `ghcr.io/pans4ge/deployra` | image prefix; `deployra` = build from the repo (`up -d --build`, updates via git) |
| `DEPLOYRA_VERSION` | `latest` | pin a version, e.g. `1.0.0` (the updater moves the pin by itself) |
| `DEPLOYRA_NAME`, `DEPLOYRA_GATEWAY`, `DEPLOYRA_JOIN` | — | joining a node; the panel gives you the values (Nodes → Add node) |
| `DEPLOYRA_UPDATE_REPO` | `PanS4ge/deployra` | where the panel checks for releases |
| `DEPLOYRA_LANG` | `pl` (panel: from the browser) | language of logs, visitor pages and the panel default (`pl` / `en`) |
| `DEPLOYRA_MAX_TRANSFER_MB` | `51200` | node: largest bundle accepted when an app moves here |
| `DEPLOYRA_PLUGINS_DIR` | `<DEPLOYRA_DATA>/plugins` | panel: plugin directory ([docs/plugins.md](../docs/plugins.md)) |
| `DEPLOYRA_GEOIP` | `on` | country and ISP of IP addresses (DB-IP Lite databases, downloaded by the gateway) |
| `DEPLOYRA_IP_RETENTION_DAYS` | `7` | how long the gateway keeps raw IP addresses |
| `DEPLOYRA_BAN_FIREWALL` | `on` | bans also in ipset/iptables, not only in nginx |
