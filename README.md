# Deployra ✦

**English** · [Polski](README.pl.md)

**Coolify for LowEnd boxes.** One place to deploy: app → domain → DNS → certificate → tunnel to your home lab.
It replaces the *FTP page + Portainer + Nginx Proxy Manager (×2 with WireGuard)* combo and runs on 512 MB of RAM.

```
                      Internet (80/443 or Cloudflare Tunnel)
                                   │
 ┌──────────── VPS ────────────────▼──────────────────────────┐
 │ Deployra-WG   WireGuard hub 10.66.0.1 · nginx · lego · cloudflared │
 └──────┬──────────────────────────────┬──────────────────────┘
        │ WG tunnel                    │ WG tunnel
 ┌──────▼─────────────┐        ┌───────▼──────────────────────┐
 │ Deployra (panel)   │──wg──► │ Deployra Backend 10.66.0.N   │
 │ anywhere           │ HMAC   │ Docker, stacks, apps,        │
 └────────────────────┘        │ Git, SFTP, terminal, Proxmox │
                               └──────────────────────────────┘
```

Public traffic takes **a single hop**: `nginx on the gateway → wg0 → node → container` (instead of NPM → NPM-with-WG → host).

## Features

| Area | What you get |
|---|---|
| **Apps** | wizard: Docker image, docker-compose, static site (ZIP + SFTP), Git repository (Dockerfile build, GitHub/Gitea webhook) · domain, Cloudflare DNS record, Let's Encrypt and a test through the tunnel in one step · rollback · optional **zero downtime** · build-time variables, `.env` editor, shared variables · scheduled tasks (cron) inside the container |
| **Docker** (instead of Portainer) | containers, live logs and stats, in-browser terminal, images, volumes, networks, compose stacks · file manager (whole folders too: upload and download as ZIP) · quick actions for databases and web servers (SQL dump, VACUUM, processes, `df -h`) |
| **Proxmox** | a backend on the Proxmox host shows **every LXC container running Docker** — no agent inside each CT |
| **Proxy** (instead of NPM) | proxy hosts, redirects, 404 hosts, TCP/UDP streams, access lists, custom locations, raw `advanced` nginx with automatic rollback · HTTP/3 (QUIC) · **Deployra login** in front of any app |
| **Import from NPM** | from `database.sqlite` (including access list passwords) or through the API; map old targets to nodes; move certificates or reissue them |
| **Tunnel** | WireGuard hub, join nodes with a token, LAN subnet routing (NAS, Home Assistant…), nodes can reach each other · several gateways, including plain nginx without WireGuard · nodes behind an **external WireGuard** (wg-easy) |
| **Cloudflare** | per-host A/AAAA records (no wildcard needed), wildcard certificates (DNS-01), **Cloudflare Tunnel** (cloudflared — no public IP, no open ports) |
| **Visits** | unique and returning visitors per domain, Cloudflare-style charts — no cookies, no bots, counted on the gateway from nginx logs |
| **Vulnerabilities (CVE)** | scans the software in containers (PHP, Node, Python, nginx, databases… from image metadata) and app dependencies (composer.lock, package-lock.json, poetry.lock…) against OSV.dev, flags ones exploited in the wild (CISA KEV), alerts and a one-click fix (redeploy with a fresh image) — no API key |
| **App updates** | new builds of the same tag (`php:8.3-apache`, `ghost:5-alpine`) and newer versions of pinned tags (`grafana:11.2.0` → `11.3.0`) — an arrow on the app icon or automatic updates, chosen in settings and per app |
| **Traffic & security** | top IP addresses with country and ISP (DB-IP Lite), suspicious addresses (floods, scanners, vulnerability probes), bans in nginx and the firewall (ipset), optional auto-ban with an allowlist |
| **Uptime** (à la Uptime Kuma) | monitors for apps, containers, nodes and URLs, 24 h / 7 d / 30 d uptime, notifications (panel, Discord, ntfy, Slack) |
| **Panel** | FastAPI + Jinja2 + htmx + Alpine (no build step), dark cosmic theme, users and permissions, SQLite or MySQL/MariaDB |
| **Languages** | Polish and English — every user picks their own in the account menu; a new language is a single JSON file |
| **Apps from files** | upload a ZIP (or use SFTP) and Deployra runs it: PHP + Apache, Python (script, Flask, FastAPI, Django), Node.js (script, Express, Next/Nuxt/Astro), Bun, Deno, Go, Rust, Ruby, Java (.jar), .NET, shell — dependencies installed automatically |
| **Resources & alerts** | CPU / RAM / disk history per node and per app, thresholds with duration, OOM and restart-loop detection, notifications to the panel, webhook, Discord, Slack, Telegram, ntfy, Gotify, Pushover or email — you pick the channels per rule |
| **Moving apps** | one click moves an app with its files, volumes and SFTP accounts to another node; the domain switches over, a failed move rolls back; optional automatic moves off a node that runs out of RAM |
| **Backups** | panel state and **app databases** (PostgreSQL, MySQL/MariaDB, MongoDB) on a schedule to S3/R2/MinIO, SFTP, FTP or disk — with retention, download and restore |
| **Map** | a graph of the whole infrastructure: panel → gateways → nodes → LXC containers → apps → domains, with the state of each element |
| **Plugins** | app templates for the wizard (Uptime Kuma, Vaultwarden, Gitea, n8n, Grafana, WordPress… built in), language packs, Python code with its own pages and event handlers |
| **Updates** | the panel checks for releases, shows an “Update available” banner and on “Update” replaces nodes, the gateway and itself in the background (or does it on its own if you enable auto-update) |

## Quick start

Step-by-step tutorial in Polish (DNS, firewall, first app, alerts): [docs/tutorial.pl.md](docs/tutorial.pl.md).

Requirements: Linux with Docker and the compose plugin. Ready-made images (amd64 + arm64) live in GHCR — nothing to build.
Which file in `deploy/` is for what: [deploy/README.md](deploy/README.md).

Add `DEPLOYRA_LANG=en` (in `deploy/.env` or in front of the commands) to get English container logs and visitor pages.

### 1. VPS — gateway and panel

```bash
git clone https://github.com/PanS4ge/deployra.git && cd deployra
DEPLOYRA_LANG=en DEPLOYRA_PUBLIC_HOST=vps.example.com docker compose -f deploy/vps.compose.yml up -d
docker logs deployra-wg | grep "Sync code"
```

Open the panel (e.g. via `ssh -L 8080:127.0.0.1:8080 vps`), finish the setup, then on **Tunnel** enter `127.0.0.1:51821`, the code,
and tick “The panel runs on the same server as the gateway”. Expose the panel publicly by adding a proxy host `deploy.example.com → 127.0.0.1:8080`.

VPS firewall ports: `80/tcp`, `443/tcp`, `443/udp` (HTTP/3), `51820/udp`, `51821/tcp` (bootstrap — only needed while nodes join).

The panel can also live elsewhere (`deploy/panel.compose.yml`): it then sets up its own tunnel to the gateway. Panel and apps on one machine (e.g. at home)
with only the gateway on the VPS: `deploy/panel-node.compose.yml` — the panel pairs the node next to it automatically.

### 2. Node (e.g. a home server)

In the panel: **Nodes → Add node → Through the tunnel** — you get a token and a ready `node.compose.yml` (that single file is enough,
no need to clone the repo). Or from the repository:

```bash
DEPLOYRA_GATEWAY=vps.example.com:51821 DEPLOYRA_JOIN=dplr1.… docker compose -f deploy/node.compose.yml up -d
```

The node generates its own WireGuard key (the private key never leaves the machine), joins the gateway, and the panel finishes pairing.

### 3. Proxmox VE

- with Docker on the host: `docker compose -f deploy/proxmox.compose.yml up -d`
- without Docker on the host: `sudo bash deploy/install-native.sh` (a systemd service, only the Docker CLI for stacks)

The backend finds running CTs (`/proc/*/cgroup`), their Docker sockets (`/proc/<pid>/root/run/docker.sock`) and names (`/etc/pve/lxc/*.conf`).
The node page gets a **Host / CT 101 · nextcloud / …** switcher. Apps deployed into a CT are reachable at the container's IP —
add its subnet under the node's “LAN subnets” so the gateway can reach it.

### 4. Node behind an external WireGuard (wg-easy)

When the server already runs its own WireGuard, use **Nodes → Add node → External WireGuard**: enter the node's address in that
network (e.g. `10.8.0.1`), the subnet and — if the node should share the network of the WireGuard container — its name (`wg-easy`).
The panel opens a page with a ready compose file (`network_mode: "container:wg-easy"` or the host network), checks that it can reach
the node API and that the gateway has a tunnel with a route to that network (**Tunnel → External WireGuard**: a wg-easy client for
the gateway), and finally pairs the node with the code from `docker logs deployra-backend`. You add the wg-easy clients yourself.

## Updates

Every 6 hours the panel checks for releases in `PanS4ge/deployra` (Settings → Updates, where you can also change the source). When a new
version appears it shows a banner: **Update / Later / Skip this version**. “Update” starts a background job: nodes first (in parallel),
then the gateway, the panel last — each step waits until the component is back on the new version. If anything fails, the panel stays put.

A node replaces itself: the backend briefly starts a copy of itself that pulls the new image, swaps the container, restores the old
one if the new version doesn't come up, and then disappears. The panel and the gateway get no `docker.sock`, so since 1.3.0 **the
Deployra node on the same machine** swaps their container (the panel sends the request, the node recreates the container on the new
version, checks the healthcheck and rolls back on failure). A node older than 1.3.0 is updated first. A gateway on a machine without
a node and older installs still use the `*-updater` container; with neither, use “Pull and redeploy”. The components table shows how
each component will be updated, and for “none” — why. Modes:

- **GHCR images** (default): `docker compose pull` + `up -d`;
- **from source** (`DEPLOYRA_IMAGES=deployra` in `deploy/.env`): `git` fast-forwards the repository to the `vX.Y.Z` tag, then `build` + `up -d`;
- **Proxmox without Docker**: `install-native.sh --update` via `systemd-run`.

### Cutting a release

1. Bump `__version__` in `shared/`, `backend/`, `gateway/` and `frontend/` (`*/__init__.py`).
2. `git tag v1.1.0 && git push origin main v1.1.0`.

GitHub Actions ([.github/workflows/build.yml](.github/workflows/build.yml)) runs the tests, builds four images for `amd64` and `arm64`,
pushes them to `ghcr.io/pans4ge/deployra-*` (`1.1.0`, `1.1`, `latest`) and creates a release with notes. A regular push only runs tests;
build `:edge` images from `main` manually (Actions → Build → Run workflow).

If the repository is private, the panel can't see releases through the GitHub API — it then checks the tags of the public
`ghcr.io/pans4ge/deployra-panel` image. The GHCR packages must be public for a plain `docker pull` without logging in.

## Languages

The panel speaks Polish and English. Each user picks a language in the account menu (stored with the account); set the default in
Settings → General or with `DEPLOYRA_LANG` — otherwise the panel follows the browser. The gateway and nodes receive the user's language
with every request, so errors and deployment logs come back in English too. `DEPLOYRA_LANG` (pl / en) also sets the language of container
logs and visitor-facing pages (“unknown host”, the placeholder of a new site).

Code and templates only contain keys (`t("nodes.add")`); the texts live in language packs:

- `frontend/deployra_panel/locales/<lang>.json` — the panel,
- `shared/deployra_common/locales/<lang>.json` — the gateway, nodes and shared code (validation, pairing).

New language: copy both `en.json` files to e.g. `de.json` and translate the values — the panel adds it to the switcher by itself.
A language can also come as a [plugin](docs/plugins.md#languages). Missing keys fall back to English, then Polish. `frontend/tests/test_i18n.py` checks that the packs have the same keys and parameters, and that no page
rendered in English contains Polish letters.

## Traffic & security

The gateway counts requests per IP from the same nginx logs as the visit statistics. The **Traffic & security** page shows:

- **top IP addresses** (24 h / 7 days, per domain) with country and ISP — from the free DB-IP Lite databases the gateway downloads monthly
  (`DEPLOYRA_GEOIP=off` disables it);
- **suspicious addresses** from the last 5 minutes: request floods (over the threshold per minute), scanners (≥ 80% 4xx) and vulnerability
  probes (`/.env`, `/wp-login.php`, `/.git/` …);
- **bans** for an address or subnet, for 1 h, a day, 7 days or permanently. nginx shows an “Access blocked” page (403), which also works for
  traffic through Cloudflare; with `ipset` available the gateway also drops packets in the firewall (`DEPLOYRA_BAN_FIREWALL=off` disables it);
- **auto-ban** (off by default) for addresses above the threshold, with an allowlist of addresses that are never banned.

Raw IPs are kept for 7 days (`DEPLOYRA_IP_RETENTION_DAYS`) — an IP address is personal data.

## App updates

**Settings → App updates** decides what Deployra does with newer images (every 3–24 h or on **Check now**):

- **Off** — no checks;
- **Show an arrow** (default) — a green arrow in the corner of the app icon, a warning on the app page, an **Update** button and a
  notification from the **App updates** alert rule;
- **Update automatically** — new builds of the same tag are deployed automatically, one by one, in a single job; with “also patch
  versions” pinned tags move within X.Y too (`11.2.0` → `11.2.3`). Deployra never makes bigger version jumps on its own.

Every app can have its own mode (app settings or the table in settings) — e.g. automatic for websites, arrow only for databases.

How it works: the node reports the digests of container images (apps from files report the language image their base image was built
from), and the panel asks the registry with a HEAD request for the tag's manifest — like Watchtower, so Docker Hub doesn't count it
against the pull limit. For a tag with an exact version it also reads the tag list and looks for newer versions of the same shape
(`29.0.7-apache` → `29.0.9-apache`, no pre-releases). Private registries use the credentials from **Settings → Registries**. An update is a
redeploy with a fresh image (in compose the exact `image:` line is replaced); apps from Git are built from a Dockerfile, so they are not
checked. `DEPLOYRA_IMAGE_CHECKS=off` turns checks off.

## Vulnerabilities (CVE)

The **Vulnerabilities** page lists known holes in what runs on your nodes — e.g. PHP2Shell (CVE-2026-109618, CVE-2026-109619) in PHP 5.4–8.5.
Every 6 hours (or on **Scan now**) the panel:

1. asks every node for a software inventory. The node runs nothing inside containers (stopped ones work too) — versions come from image
   metadata: the variables of official images (`PHP_VERSION`, `NODE_VERSION`, `PG_VERSION`, `MARIADB_VERSION`…, inherited ones too —
   WordPress and Nextcloud are built on the PHP image), the `org.opencontainers.image.version` label or an exact tag (`grafana/grafana:11.2.0`).
   For apps from files and from Git it also reads lock files: `composer.lock`, `package-lock.json`, `yarn.lock`, `requirements.txt` (`==`),
   `poetry.lock`, `uv.lock`, `Pipfile.lock`, `go.mod`, `Cargo.lock`, `Gemfile.lock`, plus what the install step actually put on disk
   (`vendor/composer/installed.json`, `node_modules/.package-lock.json`, the venv);
2. checks the versions in [OSV.dev](https://osv.dev) (software from images is the “Bitnami” ecosystem, dependencies are Packagist, npm, PyPI,
   Go, crates.io, RubyGems). No API key or account needed;
3. marks vulnerabilities from the [CISA KEV](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) catalog (exploited in the wild),
   shows the severity (from the source database or the CVSS 3 vector) and the version where everything is patched.

Critical and exploited vulnerabilities show a bar at the top of the panel, the app gets a warning on its page, and the
**Known vulnerabilities (CVE)** alert rule notifies the channels you pick (severity threshold — in the settings on the Vulnerabilities page).

**Fix** redeploys the app with a freshly pulled image: image and compose apps `pull`, Git apps build with `--pull`, and apps from files
rebuild their runtime image (e.g. `php:8.3-apache` with extensions) — otherwise that base image stays at the version from the first deploy.
Then the panel scans again. If the image has a pinned tag (e.g. `nextcloud:29.0.7`) or the branch no longer gets fixes (PHP 8.1 and older),
change the image/version in the app settings. Containers outside Deployra apps are yours to update (container page or stack).

**Ignore** hides a package until a new vulnerability shows up for it. A node that doesn't answer keeps its results unchanged (no inventory
is not a fix). `DEPLOYRA_VULN_CHECKS=off` turns off automatic scans (e.g. a panel without internet access).

Limits: the image's system packages (apt/apk, e.g. OpenSSL) are not scanned, and software without a version variable or a known tag
is not recognised.

## Deployra login in front of an app

An access list with **Require Deployra login** protects any app with a panel account (forward auth through nginx `auth_request`),
including apps without a login of their own. Access can be limited to selected users. After signing in, the panel redirects back
with a one-time token and the app gets its own cookie (works on domains other than the panel's) and an `X-Deployra-User` header with
the email. Needs HTTPS on the host and the panel domain set; it combines with Basic Auth passwords only in the “any condition” mode.

## HTTP/3

A proxy host with a certificate can turn on **HTTP/3** (QUIC): the gateway adds `listen 443 quic` and an `Alt-Svc` header, provided
its nginx has the `http_v3` module (official 1.25+ images). Open `443/udp` on the firewall; if UDP doesn't get through, browsers stay
on HTTP/2.

## Logs

**Logs** searches container logs on all nodes at once — no Elasticsearch and no extra RAM (read from Docker on demand).
Syntax: `error timeout` (all words), `"phrase"`, `/regex/`, `-word`, `app:`, `container:`, `node:`, `level:error|warn`;
range 15 min – 7 days, a hits-over-time chart, clicking a container narrows the results.

## Map

**Map** draws the infrastructure as a graph (force-graph): panel, gateways, nodes, LXC containers, apps and domains, with the state
as the ring colour and traffic flowing along the links. Clicking opens the element; free, layered or radial layout, and element
types can be hidden.

## Apps from files

**Apps → New → From files** lists ready runtimes. Pick one, upload a ZIP (or skip it and get starter files), choose the node and domain.
The files land in `/srv/deployra/sites/<slug>` on the node (also reachable over SFTP), a one-off container installs dependencies
(`pip`, `npm`, `bundle`, `go build`, `cargo`…) and the app container starts from the official language image with the files mounted.

| Runtime | Starts with | Port |
|---|---|---|
| PHP + Apache | `index.php`, `.htaccess` (mod_rewrite, mysqli, pdo_mysql) | 80 |
| Python / Flask / FastAPI / Django | `python main.py` / `gunicorn app:app` / `uvicorn main:app` / `gunicorn project.wsgi` | — / 8000 |
| Node.js / Express / Next, Nuxt, Astro | `node index.js` / `npm start` / `npm run build` + `npm start` | — / 3000 |
| Bun · Deno · Go · Rust | `bun run index.ts` · `deno run main.ts` · `go build` · `cargo build` | 3000 · 8000 · 8080 · 8080 |
| Ruby · Java · .NET · shell | `rackup` · `java -jar *.jar` · `dotnet App.dll` · `sh run.sh` | 9292 · 8080 · 8080 · — |

No port = a background process (bot, worker, script) without a domain. The start and install commands can be overridden.
`data/` (persistent data, e.g. SQLite) and `.deployra/` (caches) survive a new ZIP; a new ZIP redeploys the app (PHP picks changes up live).

## Zero-downtime deploys

With **Zero downtime** on an app (image, Git, from files — with a published port) the node starts the new container next to the old
one on a free port and waits until it runs and — if you set a **health check path** (e.g. `/up`) — answers below 500. Only then does
the panel switch the gateway to the new port; the old container goes away after 10 s (in-flight connections finish). If the new
version doesn't come up, the old one keeps serving without a gap. Off by default: for a moment both containers mount the same volumes.

## Environment variables

Every app variable has a scope: **runtime** (default), **build** (`docker build` arguments — the Dockerfile needs an `ARG`) or
**both**. Variables can be edited as a form or as `.env` text (quotes, multi-line values, comments). **Settings → Shared variables**
holds named, encrypted variable sets attached to many apps; the app's own variables win over a set, and a changed set applies on
the next deploy.

## Scheduled tasks

The **Tasks** card on the app page runs a command inside the running container (`sh -c`) on a cron schedule (`*/15 * * * *`,
`@daily`…), with a timeout, the history of the last 50 runs (exit code, duration, output) and a “Run now” button. Creating and
running tasks needs the same permission as the container terminal.

## Database backups

**Settings → Backup → Database backups**: scheduled dumps of a database container (`pg_dump`, `mysqldump`, `mongodump`) to the same
storage as the panel backup (S3/R2/MinIO, SFTP, FTP, disk), keeping the last N copies. Each schedule shows its last result and the
stored files to download or **restore** into the container; a failed dump sends an alert.

## Resources and alerts

Every minute the panel collects CPU, RAM and disk of each node and CPU/RAM of every app container: 48 hours at one-minute resolution,
30 days as hourly averages and peaks. Charts live on the node page (24 h / 7 d / 30 d) and on the app page.

**Settings → Alerts**:

- **channels**: panel, webhook (JSON, optional HMAC signature in `X-Deployra-Signature`), Discord, Slack, Telegram, ntfy, Gotify,
  Pushover, email (SMTP); every channel has a **Test** button;
- **rules**: node CPU / RAM / disk above a threshold for N minutes, node not responding, app RAM near its limit, app killed by OOM,
  restart loop, uptime monitor down, failed deployment, app moved, new version — each rule has its own threshold, duration and channels.

Protection against overflow: the first deploy checks that the node has enough free RAM (the app's limit or 64 MB) and disk, and suggests
a node with more room. The wizard shows free RAM next to every node; runtimes come with a default memory limit.

## Minecraft

The **Minecraft** section manages servers running the **DeployraBridge** plugin (Paper/Spigot/Purpur 1.8.8–1.21): console with
macros, players (game mode, heal, OP, whitelist, kick, ban), **plugins** (disable until restart or permanently, enable),
`server.properties`, files and PlaceholderAPI.

- **Server on a node**: Apps → New → **Minecraft (Paper)** (`itzg/minecraft-server`, game port 25565). The plugin downloads from the
  panel and pairs itself.
- **External hosting**: Minecraft → Add external server, install the plugin, run `deployra pair <panel-url> <code>` in the console.
  Optional hosting FTP is used for files while the server is stopped.

The server opens no port: the plugin connects to the panel itself (long polling over HTTPS) and requests and responses are signed
with HMAC and replay protection. Details: `minecraft-plugin/README.md`.

Files without FTP and without an extra port: since DeployraBridge 1.2, files over 8 MB and whole folders go through the plugin in
4 MB chunks, in the background (progress in the job, SHA-256 check and file swap at the end); downloading large files works the same
way. An older plugin can be updated from the panel (**Files** or **Settings → Update plugin**): the new jar goes to `plugins/update/`
and loads on the next server restart.

## Moving apps between nodes

**App → Move to another server**: the old node stops the app and packs its files, Docker volumes, compose stack and SFTP accounts,
the panel carries the bundle to the new node, which unpacks it, and a normal deploy follows — the proxy host gets the new address;
the domain, DNS and certificate stay. If anything fails before the switch, the app starts again on the old node. The old copy is kept
unless you tick "delete the data". Host paths mounted into an app (e.g. `/mnt/media`) are not moved — the log lists them.

Automatic moves: enable them per app (only apps without volumes and without compose) and in the **Node: RAM** rule. When a node stays
above the threshold, the biggest such app moves to the node with the most free memory (at most one app per hour per node).

## Adopting containers started outside Deployra

**Node → Containers → Adopt as an app** (or the button on the container page). Deployra reads `docker inspect`: image, command, port,
variables (without the image defaults), volumes and limits. The old container stops and stays as a copy `<name>-pre-deployra`, and an app
with the same image, data and host port takes its place — now with deployments, a domain, alerts and moving. If the new container
doesn't start, the old one comes back by itself. Containers of compose stacks are adopted as a stack (Node → Stacks).

## Accounts with assigned servers (Pterodactyl-style)

**Settings → Users → edit → Access: Assigned servers only.** Such an account only sees **My servers** in the panel — every other address
is closed (not just hidden in the menu). Next to the user (servers icon) you assign apps or containers and pick permissions for each:
**power** (start / stop / restart / kill), **logs**, **console**, **files** (ZIP upload), **deploy** and **variables** (app env).
The user sees RAM/CPU usage, status and address of their servers.

## Two gateways ("fork")

Public traffic can enter through another machine than the WireGuard hub — e.g. Oracle (80/443) + mikr.us (WireGuard). The edge
gateway joins the hub like a node (**Tunnel → Edge gateway**) and the panel moves proxy hosts, certificates, statistics and bans to it.
Step by step (Polish): [docs/tutorial.pl.md](docs/tutorial.pl.md).

## Plugins

**Settings → Plugins** lists the plugins, enables and disables them and installs new ones from a ZIP. A plugin is a directory with
`plugin.json` in `<DEPLOYRA_DATA>/plugins/` and can add:

- app templates to the wizard — the built-in *App catalog* brings Uptime Kuma, Vaultwarden, Gitea, n8n, Grafana, File Browser
  and WordPress + MariaDB, with `${RANDOM}` secrets generated on the spot;
- language packs, including new languages;
- Python code: pages with menu entries, handlers for events (deployed, failed, monitor down, new version, every notification…)
  and its own settings.

Uploaded plugins start disabled, and code plugins can only be enabled by users with the risky settings permission — plugin code runs
with the panel's privileges. Reference and examples: [docs/plugins.md](docs/plugins.md), [examples/plugins/events-log](examples/plugins/events-log)
(an event log with a webhook).

## Moving off Portainer and NPM

1. Set up the gateway and a node (above). Your existing containers show up right away under **Containers**.
2. **Import from NPM**: upload `database.sqlite` (`docker cp npm:/data/database.sqlite .`), point out that the old address (e.g. `10.8.0.2`
   from the second NPM) is now the “home” node, and import.
3. Point your domains' DNS at the gateway IP (or tick DNS records / Cloudflare Tunnel). Certificates are issued after the import.
4. Once everything works — turn off the NPMs and Portainer. 🎉

## Security

- **Sync** (the former “Diffie-Hellman”): X25519 + HKDF, messages sealed with an HMAC of a one-time code / token —
  keys can't be swapped without the code. The old DH on primes < 100 with XOR is gone.
- Every panel → gateway/node request is signed with HMAC-SHA256 (±60 s timestamp, nonce against replays); traffic goes through the WG tunnel.
- The node and gateway APIs only listen on the tunnel address; the app port pool is reachable only from `wg0` (`DOCKER-USER` rule).
- Passwords: salted scrypt. Sessions: httponly cookie + CSRF token. Secrets (Cloudflare tokens, keys) are AES-GCM encrypted in the database.
- Terminal, raw nginx, tunnel and deletion are separate (risky) permissions.

## RAM usage

Everything runs on `python:3.13-alpine` with a single uvicorn worker; heavy libraries load lazily (e.g. asyncssh only with the first SFTP account).
Compose limits: panel 160 MB, backend 192 MB, Deployra-WG 128 MB, nginx 96 MB. Measured RSS in the Windows dev environment
(CPython 3.14, with DLL overhead): panel ~87 MB, backend ~87 MB (without SFTP; docker-py weighs the most), gateway ~72 MB.
Linux/musl usually comes in lower — check `docker stats` on your box. The updater is a `sleep` loop (~1 MB + a few MB of Docker overhead per container).

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e shared fastapi uvicorn jinja2 python-multipart httpx cryptography docker psutil asyncssh pymysql loguru pyyaml websockets pytest
python scripts/dev_stack.py --reset --seed   # gateway + node (fake Docker) + panel on localhost, with sample data
```

Panel: http://127.0.0.1:8080 (`admin@deployra.local` / `kosmos-demo`). Works on Windows — no WireGuard, nginx or Docker needed.

Tests: `pytest shared/tests`, `cd backend && pytest tests`, `cd gateway && pytest tests`, `cd frontend && pytest tests`,
`bash updater/tests/test_updater.sh` (the updater with a fake Docker and a real git).

Layout: `shared/` (signatures, pairing, WireGuard, jobs, schemas, updates, i18n) · `backend/` (node) · `gateway/` (Deployra-WG) ·
`frontend/` (panel) · `updater/` (update sidecar) · `deploy/` (compose) · `docs/research/` (sources behind the decisions).
The old NiceGUI code lives on in the git history.

## Known limitations

- WireGuard, nginx, lego, cloudflared and the updater itself haven't run on a real server yet — the tests cover the logic.
- Hosts behind the Cloudflare proxy (orange cloud, not the tunnel) count visitors by Cloudflare edge addresses.
- Stacks inside a Proxmox CT: compose files live on the host, so relative path mounts won't work — use named volumes.
- Zero-downtime deploys, Deployra login, HTTP/3 and database restores are covered by tests with a fake Docker and nginx — on first
  use watch the deploy log and run `nginx -t` on the gateway.
