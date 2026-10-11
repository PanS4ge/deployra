"""Import z Nginx Proxy Managera (v2.x): baza SQLite (zalecane) albo REST API.

Schemat i zachowanie NPM opisuje docs/research/2026-10-deployra-stack.md §1:
kolumny JSON (domain_names, meta, locations), miękkie usuwanie (is_deleted),
hasła list dostępu jawnie w `access_list_auth` (API ich nie zwraca),
certyfikaty w /etc/letsencrypt/live/npm-<id>/ i /data/custom_ssl/npm-<id>/.
"""

from __future__ import annotations

import json
import posixpath
import sqlite3
import tarfile
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from deployra_common.passwords import apr1
from deployra_common.schemas import check_domain

from ..db import now
from . import proxy_state
from ..i18n import t

JSON_FIELDS = ("domain_names", "meta", "locations")


@dataclass
class NpmData:
    source: str
    proxy_hosts: list[dict] = field(default_factory=list)
    redirection_hosts: list[dict] = field(default_factory=list)
    dead_hosts: list[dict] = field(default_factory=list)
    streams: list[dict] = field(default_factory=list)
    certificates: list[dict] = field(default_factory=list)
    access_lists: list[dict] = field(default_factory=list)
    pems: dict[str, dict[str, str]] = field(default_factory=dict)  # id certyfikatu → {fullchain, privkey}
    htpasswd: dict[str, str] = field(default_factory=dict)  # id listy → zawartość pliku
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "NpmData":
        return cls(**json.loads(text))


def _decode(row: dict) -> dict:
    for key in JSON_FIELDS:
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key]) if row[key] else None
            except ValueError:
                row[key] = None
    return row


# --- źródła ---------------------------------------------------------------------------------


def read_sqlite(path: str) -> NpmData:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "proxy_host" not in tables:
        con.close()
        raise ValueError(t("npm.not_npm_db"))

    def rows(table: str) -> list[dict]:
        if table not in tables:
            return []
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        where = " WHERE is_deleted = 0" if "is_deleted" in cols else ""
        return [_decode(dict(r)) for r in con.execute(f"SELECT * FROM {table}{where} ORDER BY id")]

    data = NpmData(source="sqlite")
    data.proxy_hosts = rows("proxy_host")
    data.redirection_hosts = rows("redirection_host")
    data.dead_hosts = rows("dead_host")
    data.streams = rows("stream")
    data.certificates = rows("certificate")
    auth, clients = rows("access_list_auth"), rows("access_list_client")
    for acl in rows("access_list"):
        acl["items"] = [{"username": a["username"], "password": a.get("password") or ""} for a in auth if a["access_list_id"] == acl["id"]]
        acl["clients"] = [{"address": c["address"], "directive": c["directive"]} for c in clients if c["access_list_id"] == acl["id"]]
        data.access_lists.append(acl)
    for cert in data.certificates:
        meta = cert.get("meta") or {}
        if cert.get("provider") == "other" and meta.get("certificate") and meta.get("certificate_key"):
            chain = meta["certificate"].strip() + "\n" + (meta.get("intermediate_certificate") or "").strip()
            data.pems[str(cert["id"])] = {"fullchain": chain.strip() + "\n", "privkey": meta["certificate_key"].strip() + "\n"}
    con.close()
    return data


async def read_api(url: str, email: str, password: str, code: str = "", transport: httpx.AsyncBaseTransport | None = None) -> NpmData:
    base = url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    async with httpx.AsyncClient(base_url=base, timeout=20, transport=transport, verify=False) as client:
        response = await client.post("/api/tokens", json={"identity": email, "secret": password})
        if response.status_code >= 400:
            raise ValueError(t("npm.login_rejected", error=response.text[:200]))
        token_data = response.json()
        if token_data.get("requires_2fa"):
            if not code:
                raise ValueError(t("npm.need_2fa"))
            response = await client.post("/api/tokens/2fa", json={"challenge_token": token_data["challenge_token"], "code": code})
            if response.status_code >= 400:
                raise ValueError(t("npm.bad_2fa"))
            token_data = response.json()
        client.headers["Authorization"] = f"Bearer {token_data['token']}"

        async def get(path: str) -> list[dict]:
            r = await client.get(path)
            r.raise_for_status()
            return [_decode(item) for item in r.json()]

        data = NpmData(source="api")
        data.proxy_hosts = await get("/api/nginx/proxy-hosts")
        data.redirection_hosts = await get("/api/nginx/redirection-hosts")
        data.dead_hosts = await get("/api/nginx/dead-hosts")
        data.streams = await get("/api/nginx/streams")
        data.certificates = await get("/api/nginx/certificates")
        data.access_lists = await get("/api/nginx/access-lists?expand=items,clients")
    for acl in data.access_lists:
        if any(not item.get("password") for item in acl.get("items") or []):
            data.warnings.append(t("npm.acl_no_passwords", name=acl.get("name")))
    return data


def read_archive(data: NpmData, path: str) -> None:
    """tar(.gz) z katalogami NPM (/data i/lub /etc/letsencrypt): certyfikaty i pliki htpasswd."""
    with tarfile.open(path) as tar:
        members = {m.name.lstrip("./"): m for m in tar.getmembers()}

        def read(name: str, depth: int = 0) -> str | None:
            member = members.get(name)
            if member is None or depth > 8:
                return None
            if member.issym():
                return read(posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname)), depth + 1)
            if member.islnk():
                return read(member.linkname.lstrip("./"), depth + 1)
            if not member.isfile() or member.size > 1_000_000:
                return None
            fh = tar.extractfile(member)
            return fh.read().decode(errors="replace") if fh else None

        def find(suffix: str) -> str | None:
            for name in members:
                if name == suffix or name.endswith("/" + suffix):
                    return read(name)
            return None

        for cert in data.certificates:
            cid = str(cert["id"])
            for folder in (f"live/npm-{cid}", f"custom_ssl/npm-{cid}"):
                chain, key = find(f"{folder}/fullchain.pem"), find(f"{folder}/privkey.pem")
                if chain and key:
                    data.pems[cid] = {"fullchain": chain, "privkey": key}
                    break
        for acl in data.access_lists:
            content = find(f"access/{acl['id']}")
            if content:
                data.htpasswd[str(acl["id"])] = content


# --- analiza i zastosowanie --------------------------------------------------------------------


def split_host(value: str) -> tuple[str, str]:
    """NPM pozwala na `host/ścieżka` w forward_host."""
    host, _, path = (value or "").partition("/")
    return host.strip(), ("/" + path) if path else ""


def forward_hosts(data: NpmData) -> list[dict]:
    seen: dict[str, dict] = {}
    for host in data.proxy_hosts:
        name, _ = split_host(host.get("forward_host", ""))
        entry = seen.setdefault(name, {"host": name, "count": 0, "examples": []})
        entry["count"] += 1
        entry["examples"].append((host.get("domain_names") or ["?"])[0])
        for loc in host.get("locations") or []:
            loc_name, _ = split_host(loc.get("forward_host", ""))
            seen.setdefault(loc_name, {"host": loc_name, "count": 0, "examples": []})["count"] += 1
    for stream in data.streams:
        name = stream.get("forwarding_host") or stream.get("forward_ip") or ""
        seen.setdefault(name, {"host": name, "count": 0, "examples": []})["count"] += 1
    return sorted(seen.values(), key=lambda e: -e["count"])


def _target(mapping: dict[str, Any], host: str, port: int, scheme: str, path: str) -> dict:
    rule = mapping.get(host) or {"type": "address"}
    if rule.get("type") == "node" and rule.get("node_id"):
        return {"type": "node", "node_id": int(rule["node_id"]), "port": int(port), "scheme": scheme or "http", "path": path}
    return {"type": "address", "host": host, "port": int(port), "scheme": scheme or "http", "path": path,
            "via_node": int(rule["via_node"]) if rule.get("via_node") else None}  # fmt: skip


def _flags(item: dict) -> dict:
    get = lambda key: int(bool(item.get(key)))  # noqa: E731
    return {
        "ssl_forced": get("ssl_forced"), "http2": get("http2_support"), "hsts": get("hsts_enabled"),
        "hsts_subdomains": get("hsts_subdomains"), "block_exploits": get("block_exploits"),
        "websockets": get("allow_websocket_upgrade"), "caching": get("caching_enabled"),
        "trust_forwarded_proto": get("trust_forwarded_proto"), "enabled": int(item.get("enabled", 1) not in (0, False)),
        "advanced": item.get("advanced_config") or "",
    }  # fmt: skip


def apply(panel: Any, data: NpmData, mapping: dict[str, Any], options: dict[str, Any], log) -> dict[str, Any]:
    """Tworzy obiekty w panelu. Zwraca raport i listę certyfikatów do wystawienia / wgrania."""
    db = panel.db
    existing = {d for s in db.all("sites") for d in s["domains"]}
    report = {"sites": 0, "redirects": 0, "dead": 0, "streams": 0, "acls": 0, "skipped": [], "reissue": [], "upload": []}
    acl_map: dict[int, int] = {}
    cert_map: dict[int, int] = {}

    if options.get("acls", True):
        for acl in data.access_lists:
            old_hashes = {}
            for line in (data.htpasswd.get(str(acl["id"])) or "").splitlines():
                user, sep, digest = line.partition(":")
                if sep:
                    old_hashes[user.strip()] = digest.strip()
            users = []
            for item in acl.get("items") or []:
                if item.get("password"):
                    users.append({"username": item["username"], "htpasswd": apr1(item["password"])})
                elif item["username"] in old_hashes:
                    users.append({"username": item["username"], "htpasswd": old_hashes[item["username"]]})
                else:
                    log(t("npm.acl_user_skipped", name=acl.get("name"), user=item["username"]))
            clients = [{"address": c["address"], "directive": c.get("directive", "allow")} for c in acl.get("clients") or []]
            acl_map[acl["id"]] = db.insert("access_lists", name=str(acl.get("name") or f"npm-{acl['id']}")[:80],
                                           satisfy_any=int(bool(acl.get("satisfy_any"))), pass_auth=int(bool(acl.get("pass_auth"))),
                                           users=users, clients=clients, created_at=now())  # fmt: skip
            report["acls"] += 1

    cert_mode = options.get("certs", "reissue")
    if cert_mode != "skip":
        for cert in data.certificates:
            domains = [d for d in (cert.get("domain_names") or []) if d]
            if not domains:
                continue
            pem = data.pems.get(str(cert["id"]))
            wildcard = any(d.startswith("*.") for d in domains)
            if cert_mode == "pem" and pem:
                cid = db.insert("certificates", name=cert.get("nice_name") or domains[0], domains=domains, challenge="custom",
                                status="new", created_at=now())  # fmt: skip
                report["upload"].append({"id": cid, **pem})
            elif cert.get("provider") == "letsencrypt" or (cert_mode == "pem" and not pem):
                cid = db.insert("certificates", name=cert.get("nice_name") or domains[0], domains=domains,
                                challenge="dns-cloudflare" if wildcard else "http", status="new", created_at=now())  # fmt: skip
                report["reissue"].append(cid)
            else:
                log(t("npm.cert_no_pem", name=cert.get("nice_name")))
                continue
            cert_map[cert["id"]] = cid

    def common(item: dict) -> dict:
        values = _flags(item)
        values["cert_id"] = cert_map.get(item.get("certificate_id") or 0)
        if not values["cert_id"]:
            values["ssl_forced"] = 0
        values["access_list_id"] = acl_map.get(item.get("access_list_id") or 0)
        values["source"] = "npm"
        values["created_at"] = now()
        return values

    def domains_of(item: dict) -> list[str] | None:
        try:
            domains = [check_domain(d) for d in item.get("domain_names") or []]
        except ValueError as exc:
            report["skipped"].append(f"{item.get('domain_names')}: {exc}")
            return None
        clash = existing.intersection(domains)
        if clash:
            report["skipped"].append(t("npm.domain_exists", domains=", ".join(clash)))
            return None
        existing.update(domains)
        return domains

    if options.get("proxy", True):
        for item in data.proxy_hosts:
            domains = domains_of(item)
            if not domains:
                continue
            host, path = split_host(item.get("forward_host", ""))
            locations = []
            for loc in item.get("locations") or []:
                lhost, lpath = split_host(loc.get("forward_host", ""))
                target = _target(mapping, lhost, loc.get("forward_port") or 80, loc.get("forward_scheme"), lpath or loc.get("forward_path") or "")
                if target["type"] == "node":
                    node = panel.node(target["node_id"])
                    target = {"type": "address", "host": panel.node_upstream_host(node) or lhost, "port": target["port"],
                              "scheme": target["scheme"], "path": target["path"]}  # fmt: skip
                locations.append({"path": loc.get("path") or "/", "target": target, "advanced": loc.get("advanced_config") or ""})
            db.insert("sites", kind="proxy", domains=domains,
                      target=_target(mapping, host, item.get("forward_port") or 80, item.get("forward_scheme"), path),
                      locations=locations, **common(item))  # fmt: skip
            report["sites"] += 1

    if options.get("redirects", True):
        for item in data.redirection_hosts:
            domains = domains_of(item)
            if not domains:
                continue
            scheme = item.get("forward_scheme") or "auto"
            redirect = {"scheme": scheme if scheme in ("http", "https") else "auto", "domain": item.get("forward_domain_name"),
                        "code": int(item.get("forward_http_code") or 302), "preserve_path": bool(item.get("preserve_path"))}  # fmt: skip
            if redirect["code"] not in (301, 302, 307, 308):
                redirect["code"] = 302
            db.insert("sites", kind="redirect", domains=domains, redirect=redirect, **common(item))
            report["redirects"] += 1

    if options.get("dead", True):
        for item in data.dead_hosts:
            domains = domains_of(item)
            if domains:
                db.insert("sites", kind="dead", domains=domains, **common(item))
                report["dead"] += 1

    if options.get("streams", True):
        used_ports = {s["listen_port"] for s in db.all("streams")}
        for item in data.streams:
            port = int(item.get("incoming_port") or 0)
            if port in used_ports or port in (80, 443, 51820, 51821, 2751):
                report["skipped"].append(t("npm.stream_port_taken", port=port))
                continue
            host = item.get("forwarding_host") or item.get("forward_ip") or ""
            rule = mapping.get(host) or {}
            node_id = int(rule["node_id"]) if rule.get("type") == "node" and rule.get("node_id") else None
            db.insert("streams", listen_port=port, node_id=node_id, upstream_host=host or "127.0.0.1",
                      upstream_port=int(item.get("forwarding_port") or 0), tcp=int(bool(item.get("tcp_forwarding", 1))),
                      udp=int(bool(item.get("udp_forwarding"))), enabled=int(item.get("enabled", 1) not in (0, False)),
                      created_at=now())  # fmt: skip
            used_ports.add(port)
            report["streams"] += 1
    return report


async def run(panel: Any, job: Any, data: NpmData, mapping: dict, options: dict) -> dict:
    from ..services import certs as certs_svc

    job.log(t("npm.summary", source=data.source, proxy=len(data.proxy_hosts), redirects=len(data.redirection_hosts),
              dead=len(data.dead_hosts), streams=len(data.streams), acls=len(data.access_lists)))  # fmt: skip
    for warning in data.warnings:
        job.log(f"⚠ {warning}")
    report = apply(panel, data, mapping, options, job.log)
    job.log(t("npm.created", proxy=report["sites"], redirects=report["redirects"], dead=report["dead"],
              streams=report["streams"], acls=report["acls"]))  # fmt: skip
    for line in report["skipped"]:
        job.log(t("npm.skipped", line=line))

    client = panel.gateway_client()
    for cert in report["upload"]:
        if not client:
            break
        try:
            result = await client.post("/api/certs/upload", json={"id": cert["id"], "fullchain": cert["fullchain"], "privkey": cert["privkey"]})
            info = result.get("cert") or {}
            panel.db.update("certificates", cert["id"], status="valid", expires_at=info.get("not_after"))
            job.log(t("npm.cert_uploaded", id=cert["id"]))
        except Exception as exc:  # noqa: BLE001 — raportujemy i jedziemy dalej
            panel.db.update("certificates", cert["id"], status="error", error=str(getattr(exc, "detail", exc)))
            job.log(t("npm.cert_failed", id=cert["id"], error=getattr(exc, "detail", exc)))

    result = await proxy_state.push(panel)
    message, _ = proxy_state.push_message(result)
    job.log(message)

    if report["reissue"]:
        if not client:
            job.log(t("npm.no_gateway_certs"))
        else:
            job.log(t("npm.reissuing", count=len(report["reissue"])))
            for cert_id in report["reissue"]:
                try:
                    await certs_svc.issue(panel, job, cert_id)
                except Exception as exc:  # noqa: BLE001
                    job.log(t("npm.cert_failed", id=cert_id, error=exc))
            message, _ = proxy_state.push_message(await proxy_state.push(panel))
            job.log(message)
    job.log(t("npm.done"))
    return {k: v for k, v in report.items() if k != "upload"}
