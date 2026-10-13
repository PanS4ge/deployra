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

Two gateways ("fork"): public traffic on one VPS (e.g. Oracle), WireGuard on another (e.g. mikr.us). The edge gateway is the same
`gateway.compose.yml` with `DEPLOYRA_HUB` and `DEPLOYRA_JOIN` (the panel gives you the command: **Tunnel → Edge gateway**):

```
 Oracle:   gateway.compose.yml + DEPLOYRA_HUB=srv12.mikr.us:20123 DEPLOYRA_JOIN=…   (80/443, certificates, statistics)
 mikr.us:  gateway.compose.yml + DEPLOYRA_WG_PORT / DEPLOYRA_BOOTSTRAP_PORT        (WireGuard hub)
 Home:     panel-node.compose.yml  or  node.compose.yml
```

Panel at home next to the apps, gateway alone on the VPS:

```
 VPS:    gateway.compose.yml
 Home:   panel-node.compose.yml  = panel.compose.yml + node.compose.yml (paired automatically)
```

## Paste-ready stacks (`deploy/stacks/`)

Self-contained files with no `build:` and no references to other files — paste them into Portainer (**Stacks → Add stack → Web editor**)
or run `docker compose -f <file> up -d`. Only change the lines marked `←`. Images are pulled from GHCR.

| File | What it starts |
|---|---|
| `stacks/vps.yml` | gateway + panel on one VPS |
| `stacks/gateway.yml` | just the gateway (also a hub on mikr.us or an edge gateway on Oracle) |
| `stacks/panel-node.yml` | panel + node on one machine (the panel pairs the node by itself) |
| `stacks/node.yml` | just a node (token from the panel) |

No updater containers: the node updates itself from the panel; for the panel and the gateway use **Pull and redeploy** in Portainer
(or `docker compose pull && docker compose up -d`).

## Updater (`*-updater` services)

The panel shows an “Update available” banner. After you click “Update” it asks the nodes, the gateway and finally itself to update.

- **A node** needs no updater: the backend has `docker.sock` anyway, so it briefly starts a copy of itself
  (`deployra-backend-update`) that pulls the new image, replaces the container (same variables, volumes, network and stack labels),
  waits for the new version to come up and restores the old one if it doesn't. Then the copy is gone.
  Works for Portainer stacks too. A version pinned in `.env` (`DEPLOYRA_VERSION`) isn't moved — change it by hand.
- **The panel and the gateway** get no access to Docker, so a small `deployra-*-updater` container sits next to them
  (~1 MB RAM when idle, no network, only `docker.sock`).

## External WireGuard (wg-easy, home router)

If your home machines already sit in another WireGuard server's network (e.g. `10.8.0.0/24` behind wg-easy on a separate VPS),
the gateway can join it as a regular client: **Network → External WireGuard**, paste the client's `.conf` file (in wg-easy: new
client → download). Proxy hosts can then point straight at `10.8.0.x` addresses — no Deployra needed on those machines.

- Only the listed subnets go through the tunnel (by default the one from `Address`). wg-easy's `AllowedIPs = 0.0.0.0/0` is
  dropped — otherwise all of the gateway's traffic would go through someone else's server. `DNS` and `PostUp/PostDown` are dropped too.
- The configuration lives in the gateway's data, so the tunnel comes back by itself after a restart and after “Pull and redeploy”.
- An interface brought up earlier by hand under the same name (e.g. `wg1`) is taken over.
- A subnet can't also be assigned to a Deployra peer — remove it from the peer first (or remove the dead peer altogether).

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
| `DEPLOYRA_VERSION` | `latest` | pin a version, e.g. `1.0.0` (the panel's and gateway's updater moves the pin by itself) |
| `DEPLOYRA_NAME`, `DEPLOYRA_GATEWAY`, `DEPLOYRA_JOIN` | — | joining a node; the panel gives you the values (Nodes → Add node) |
| `DEPLOYRA_UPDATE_REPO` | `PanS4ge/deployra` | where the panel checks for releases |
| `DEPLOYRA_LANG` | `pl` (panel: from the browser) | language of logs, visitor pages and the panel default (`pl` / `en`) |
| `DEPLOYRA_HUB`, `DEPLOYRA_JOIN` | — | edge gateway: WireGuard hub address and the token from the panel (Tunnel → Edge gateway) |
| `DEPLOYRA_WG_PORT`, `DEPLOYRA_BOOTSTRAP_PORT` | `51820`, `51821` | gateway ports (e.g. the ones mikr.us assigns) |
| `DEPLOYRA_MAX_TRANSFER_MB` | `51200` | node: largest bundle accepted when an app moves here |
| `DEPLOYRA_PLUGINS_DIR` | `<DEPLOYRA_DATA>/plugins` | panel: plugin directory ([docs/plugins.md](../docs/plugins.md)) |
| `DEPLOYRA_GEOIP` | `on` | country and ISP of IP addresses (DB-IP Lite databases, downloaded by the gateway) |
| `DEPLOYRA_IP_RETENTION_DAYS` | `7` | how long the gateway keeps raw IP addresses |
| `DEPLOYRA_BAN_FIREWALL` | `on` | bans also in ipset/iptables, not only in nginx |
