"""Sterownik nginx: neutralny `ProxyState` → pliki konfiguracji.

Panel wysyła model (hosty, przekierowania, 404, streamy, listy dostępu),
a ten moduł zamienia go w konfigurację nginx. Wartości trafiające do plików
są wcześniej walidowane w `deployra_common.schemas`; pola `advanced` to
celowo surowy nginx (jak w Nginx Proxy Managerze).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deployra_common import i18n
from deployra_common.schemas import AccessList, ProxyState, Site, Stream, Upstream

ROOT = "/etc/nginx/deployra"

SNIPPETS: dict[str, str] = {
    "acme.conf": """# Wyzwania ACME (HTTP-01) — zawsze publiczne, bez list dostępu
location ^~ /.well-known/acme-challenge/ {
    auth_basic off;
    allow all;
    default_type "text/plain";
    root /var/www/acme;
}
""",
    "proxy.conf": """proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Port $server_port;
proxy_connect_timeout 15s;
proxy_send_timeout 300s;
proxy_read_timeout 300s;
""",
    "ssl.conf": """ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:DeployraSSL:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;
""",
    "block-exploits.conf": """# Blokowanie prostych ataków (na wzór NPM)
set $deployra_block 0;
if ($query_string ~ "[a-zA-Z0-9_]=(https?|ftp)://") { set $deployra_block 1; }
if ($query_string ~ "(\\.\\./|%2e%2e%2f|%2e%2e/)") { set $deployra_block 1; }
if ($query_string ~* "(<|%3C).*script.*(>|%3E)") { set $deployra_block 1; }
if ($query_string ~* "union.*select.*\\(") { set $deployra_block 1; }
if ($query_string ~* "(GLOBALS|_REQUEST)(=|\\[|%[0-9A-Z]{0,2})") { set $deployra_block 1; }
if ($request_uri ~* "/(\\.git|\\.env|\\.svn|\\.hg)(/|$)") { set $deployra_block 1; }
if ($http_user_agent ~* "(sqlmap|nikto|masscan|zgrab|nmap)") { set $deployra_block 1; }
if ($deployra_block = 1) { return 403; }
""",
    "cache.conf": """proxy_cache_valid 200 302 10m;
proxy_cache_valid 404 1m;
proxy_cache deployra_cache;
proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
add_header X-Deployra-Cache $upstream_cache_status;
""",
}

def deployra_page() -> str:
    """Kosmiczna strona „nieznany host” — dla odwiedzających, więc w stałym języku bramki (DEPLOYRA_LANG)."""
    lang = i18n.default_language()
    title, text = (i18n.server.raw(f"gw.unknown_host.{key}", lang).replace('"', "'") for key in ("title", "text"))
    return (
        f"<!DOCTYPE html><html lang={lang}><meta charset=utf-8><title>Deployra</title>"
        "<body style='margin:0;min-height:100vh;display:grid;place-items:center;background:#05060f;color:#e6e8ff;"
        f"font-family:system-ui'><div style=text-align:center><h1>{title}</h1>"
        f"<p style=color:#8b90b8>{text}</p></div></body></html>"
    )


BAN_CHECK = "if ($deployra_ban) {\n    return 444;\n}"  # zbanowany adres: zamknij połączenie bez odpowiedzi


def _ind(text: str, level: int = 1) -> str:
    pad = "    " * level
    return "\n".join(pad + line if line.strip() else "" for line in text.strip("\n").splitlines())


def _listen(port: int, ipv6: bool, extra: str = "", default: bool = False) -> list[str]:
    suffix = (" default_server" if default else "") + (f" {extra}" if extra else "")
    lines = [f"listen {port}{suffix};"]
    if ipv6:
        lines.append(f"listen [::]:{port}{suffix};")
    return lines


def _upstream_url(up: Upstream) -> str:
    host = f"[{up.host}]" if ":" in up.host else up.host
    return f"{up.scheme}://{host}:{up.port}{up.path}"


def _access_lines(acl: AccessList | None, root: str) -> list[str]:
    if acl is None:
        return []
    lines = []
    if acl.users:
        lines += ['auth_basic "Dostep chroniony";', f"auth_basic_user_file {root}/access/{acl.id}.htpasswd;"]
        if not acl.pass_auth:
            lines.append('proxy_set_header Authorization "";')
    for client in acl.clients:
        lines.append(f"{client.directive} {client.address};")
    if acl.clients and not any(c.address == "all" for c in acl.clients):
        lines.append("deny all;")
    if acl.users and acl.clients:
        lines.append(f"satisfy {'any' if acl.satisfy_any else 'all'};")
    return lines


def _proxy_location(path: str, up: Upstream, site: Site, acl_lines: list[str], advanced: str = "") -> str:
    body = [f"proxy_pass {_upstream_url(up)};", f"include {ROOT}/snippets/proxy.conf;"]
    body.append(f"proxy_set_header X-Forwarded-Proto {'$deployra_xfp' if site.trust_forwarded_proto else '$scheme'};")
    if site.websockets:
        body += ["proxy_set_header Upgrade $http_upgrade;", "proxy_set_header Connection $connection_upgrade;"]
    else:
        body.append('proxy_set_header Connection "";')
    if up.scheme == "https":
        body += ["proxy_ssl_server_name on;", "proxy_ssl_name $host;"]
    if site.caching:
        body.append(f"include {ROOT}/snippets/cache.conf;")
    body += acl_lines
    text = f"location {path} {{\n{_ind(chr(10).join(body))}"
    if advanced.strip():
        text += "\n" + _ind(advanced)
    return text + "\n}"


def _site_body(site: Site, acl: AccessList | None, root: str) -> str:
    parts = []
    acl_lines = _access_lines(acl, root)
    if site.kind == "proxy":
        parts.append(f"client_max_body_size {site.max_body_mb}m;")
        if site.block_exploits:
            parts.append(f"include {root}/snippets/block-exploits.conf;")
        assert site.upstream is not None
        if site.advanced.strip():
            parts.append(site.advanced.strip())
        custom_paths = {loc.path for loc in site.locations}
        if "/" not in custom_paths:
            parts.append(_proxy_location("/", site.upstream, site, acl_lines))
        for loc in site.locations:
            parts.append(_proxy_location(loc.path, loc.upstream, site, acl_lines, loc.advanced))
    elif site.kind == "redirect":
        assert site.redirect is not None
        r = site.redirect
        scheme = "$scheme" if r.scheme == "auto" else r.scheme
        target = f"{scheme}://{r.domain}" + ("$request_uri" if r.preserve_path else "")
        if site.block_exploits:
            parts.append(f"include {root}/snippets/block-exploits.conf;")
        if site.advanced.strip():
            parts.append(site.advanced.strip())
        parts.append(f"location / {{\n{_ind(chr(10).join(acl_lines + [f'return {r.code} {target};']))}\n}}")
    else:  # dead
        if site.advanced.strip():
            parts.append(site.advanced.strip())
        parts.append("location / {\n    return 404;\n}")
    return "\n".join(parts)


def render_site(site: Site, acl: AccessList | None, cert_ready: bool, ipv6: bool, root: str = ROOT) -> str:
    names = " ".join(site.domains)
    out = [f"# Deployra: {site.kind} #{site.id} — {names}"]
    body = _site_body(site, acl, root)
    http_lines = _listen(80, ipv6) + [f"server_name {names};", BAN_CHECK, f"include {root}/snippets/acme.conf;"]
    if cert_ready and site.ssl_forced:
        http_lines.append("location / {\n    return 301 https://$host$request_uri;\n}")
    else:
        http_lines.append(body)
    out.append("server {\n" + _ind("\n".join(http_lines)) + "\n}")
    if cert_ready and site.cert_id is not None:
        tls = _listen(443, ipv6, "ssl")
        if site.http2:
            tls.append("http2 on;")
        tls += [
            f"server_name {names};",
            BAN_CHECK,
            f"ssl_certificate /etc/deployra/certs/{site.cert_id}/fullchain.pem;",
            f"ssl_certificate_key /etc/deployra/certs/{site.cert_id}/privkey.pem;",
            f"include {root}/snippets/ssl.conf;",
        ]
        if site.hsts:
            tls.append(
                'add_header Strict-Transport-Security "max-age=63072000'
                + ("; includeSubDomains" if site.hsts_subdomains else "")
                + '" always;'
            )
        tls.append(body)
        out.append("server {\n" + _ind("\n".join(tls)) + "\n}")
    return "\n\n".join(out) + "\n"


def render_bans(bans: list[str]) -> str:
    """Zmienna $deployra_ban (1 = zbanowany) — plik ładuje się przed serwerami (00-bans < 00-default)."""
    lines = ["# Deployra: zbanowane adresy", "geo $deployra_ban {", "    default 0;"]
    lines += [f"    {net} 1;" for net in bans]
    return "\n".join(lines + ["}"]) + "\n"


def render_stream(stream: Stream, ipv6: bool, bans: list[str] = ()) -> str:
    host = f"[{stream.upstream_host}]" if ":" in stream.upstream_host else stream.upstream_host
    out = [f"# Deployra: stream #{stream.id} — :{stream.listen_port} → {host}:{stream.upstream_port}"]
    for proto, enabled in (("", stream.tcp), (" udp", stream.udp)):
        if enabled:
            lines = _listen(stream.listen_port, ipv6, proto.strip()) + [f"deny {net};" for net in bans]
            lines.append(f"proxy_pass {host}:{stream.upstream_port};")
            out.append("server {\n" + _ind("\n".join(lines)) + "\n}")
    return "\n\n".join(out) + "\n"


def render_default(ipv6: bool, page: str, root: str = ROOT) -> str:
    body = (
        f"default_type text/html;\n    return 404 \"{deployra_page()}\";"
        if page == "deployra"
        else "return 404;"
    )
    http = _listen(80, ipv6, default=True) + ["server_name _;", BAN_CHECK, f"include {root}/snippets/acme.conf;"]
    https = _listen(443, ipv6, "ssl", default=True) + ["server_name _;", "ssl_reject_handshake on;"]
    return (
        "# Deployra: serwer domyślny\n"
        "server {\n" + _ind("\n".join(http)) + f"\n    location / {{\n        {body}\n    }}\n}}\n\n"
        "server {\n" + _ind("\n".join(https)) + "\n}\n"
    )


def render(state: ProxyState, cert_ready: Callable[[int], bool], ipv6: bool, root: str = ROOT, bans: list[str] = ()) -> dict[str, str]:
    """Zwraca {ścieżka względna: treść} dla katalogów http/, stream/, access/."""
    acls = {acl.id: acl for acl in state.access_lists}
    files: dict[str, str] = {"http/00-bans.conf": render_bans(list(bans)), "http/00-default.conf": render_default(ipv6, state.default_page, root)}
    for site in state.sites:
        if not site.enabled:
            continue
        acl = acls.get(site.access_list_id) if site.access_list_id is not None else None
        ready = site.cert_id is not None and cert_ready(site.cert_id)
        files[f"http/site-{site.id}.conf"] = render_site(site, acl, ready, ipv6, root)
    for stream in state.streams:
        if stream.enabled:
            files[f"stream/stream-{stream.id}.conf"] = render_stream(stream, ipv6, list(bans))
    for acl in state.access_lists:
        if acl.users:
            files[f"access/{acl.id}.htpasswd"] = "".join(f"{u.username}:{u.htpasswd}\n" for u in acl.users)
    return files


def write_snippets(nginx_dir: Path) -> None:
    snippets = nginx_dir / "snippets"
    snippets.mkdir(parents=True, exist_ok=True)
    for name, content in SNIPPETS.items():
        (snippets / name).write_text(content, encoding="utf-8")
