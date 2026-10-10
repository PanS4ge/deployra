"""Proxy (zamiennik Nginx Proxy Managera): hosty, przekierowania, 404, streamy, listy dostępu."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from deployra_common.passwords import apr1
from deployra_common.schemas import AccessClient, AccessUser, Redirect, Stream, Upstream, check_domain

from ..context import Panel, get_panel
from ..db import now
from ..deps import current_user, require
from ..permissions import Permission, has
from ..services import certs as certs_svc
from ..services import dns, proxy_state
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
KIND_SLUG = {"hosts": "proxy", "redirects": "redirect", "dead": "dead"}
SLUG_OF = {v: k for k, v in KIND_SLUG.items()}
KIND_TITLE = {"proxy": "Hosty proxy", "redirect": "Przekierowania", "dead": "Hosty 404"}
FLAGS = ("ssl_forced", "http2", "hsts", "hsts_subdomains", "block_exploits", "websockets", "caching", "trust_forwarded_proto", "enabled", "dns")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r\n", "\n").replace(",", "\n").splitlines() if line.strip()]


def _errors(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "\n".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors())
    return str(exc)


async def save_and_push(panel: Panel, table: str, row_id: int | None, values: dict[str, Any]) -> tuple[int, dict]:
    """Zapisuje wiersz i wysyła stan do bramki; jeśli nginx odrzuci — przywraca poprzedni stan."""
    old = panel.db.get(table, row_id) if row_id else None
    if old:
        panel.db.update(table, row_id, **values)
    else:
        row_id = panel.db.insert(table, created_at=now(), **values)
    result = await proxy_state.push(panel)
    if result.get("status") == "rejected":
        if old:
            panel.db.update(table, row_id, **{k: old[k] for k in values})
        else:
            panel.db.delete(table, row_id)
        await proxy_state.push(panel)
        raise LookupError(f"nginx odrzucił konfigurację — przywrócono poprzednią:\n{result.get('message')}")
    return row_id, result


# --- listy ------------------------------------------------------------------------------------


@router.get("/proxy")
def proxy_index(request: Request):
    return redirect(request, "/proxy/hosts")


@router.get("/proxy/streams")
def streams(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("streams")
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    return render(request, "proxy/streams.html", title="Streamy TCP/UDP", tab="streams", streams=rows, nodes=nodes,
                  node_list=list(nodes.values()))  # fmt: skip


@router.get("/proxy/access")
def access_lists(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("access_lists")
    used = {}
    for site in panel.db.all("sites"):
        if site.get("access_list_id"):
            used[site["access_list_id"]] = used.get(site["access_list_id"], 0) + 1
    return render(request, "proxy/access.html", title="Listy dostępu", tab="access", lists=rows, used=used)


@router.get("/proxy/{kind_slug}")
def sites(request: Request, kind_slug: str, panel: Panel = Depends(get_panel)):
    kind = KIND_SLUG.get(kind_slug)
    if not kind:
        raise HTTPException(404, "Nie ma takiej sekcji")
    rows = panel.db.fetchall("SELECT * FROM sites WHERE kind = ? ORDER BY id", [kind])
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    certs = {c["id"]: c for c in panel.db.all("certificates")}
    apps = {a["id"]: a for a in panel.db.all("apps")}
    return render(request, "proxy/sites.html", title=KIND_TITLE[kind], tab=kind_slug, kind=kind, sites=rows, nodes=nodes,
                  certs=certs, apps=apps)  # fmt: skip


# --- formularz hosta ---------------------------------------------------------------------------


def _form_context(panel: Panel, site: dict | None, kind: str) -> dict:
    return {
        "site": site or {"kind": kind, "domains": [], "target": {"type": "node", "scheme": "http"}, "redirect": {}, "locations": [],
                         "http2": 1, "block_exploits": 1, "websockets": 1, "enabled": 1, "max_body_mb": 100,
                         "dns": int(bool(panel.db.setting("cloudflare_token")))},
        "kind": kind,
        "nodes": panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' ORDER BY name"),
        "certs": panel.db.all("certificates"),
        "acls": panel.db.all("access_lists"),
        "has_cf": bool(panel.db.setting("cloudflare_token")),
    }  # fmt: skip


@router.get("/proxy/hosts/new")
def new_site(request: Request, kind: str = "proxy", panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    if kind not in KIND_TITLE:
        kind = "proxy"
    return render(request, "proxy/site_form.html", title=f"Nowy: {KIND_TITLE[kind].lower()}", **_form_context(panel, None, kind))


@router.get("/proxy/hosts/{site_id}")
def edit_site(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, "Nie ma takiego hosta")
    return render(request, "proxy/site_form.html", title=", ".join(site["domains"][:2]), **_form_context(panel, site, site["kind"]))


def _target_from(form: Any, prefix: str = "") -> dict:
    kind = str(form.get(f"{prefix}target_type") or "node")
    scheme = str(form.get(f"{prefix}scheme") or "http")
    port = int(form.get(f"{prefix}port") or 0)
    path = str(form.get(f"{prefix}path") or "").strip()
    if kind == "node":
        node_id = int(form.get(f"{prefix}node_id") or 0)
        if not node_id:
            raise ValueError("wybierz węzeł")
        Upstream(scheme=scheme, host="127.0.0.1", port=port, path=path)  # walidacja portu/ścieżki
        return {"type": "node", "node_id": node_id, "port": port, "scheme": scheme, "path": path}
    host = str(form.get(f"{prefix}host") or "").strip()
    up = Upstream(scheme=scheme, host=host, port=port, path=path)
    via = int(form.get(f"{prefix}via_node") or 0) or None
    return {"type": "address", "host": up.host, "port": port, "scheme": scheme, "path": path, "via_node": via}


def _locations_from(text: str) -> list[dict]:
    """Linie: `/ścieżka http://host:port/ścieżka`."""
    out = []
    for line in str(text or "").replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, url = line.partition(" ")
        url = url.strip()
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise ValueError(f"lokacja „{line}” — oczekiwano: /ścieżka http://host:port")
        up = Upstream(scheme=parsed.scheme, host=parsed.host, port=parsed.port or (443 if parsed.scheme == "https" else 80),
                      path=parsed.path if parsed.path != "/" else "")  # fmt: skip
        out.append({"path": path, "target": {"type": "address", **up.model_dump()}, "advanced": ""})
    return out


@router.post("/proxy/hosts/save")
async def save_site(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    form = await request.form()
    site_id = int(form.get("id") or 0) or None
    kind = str(form.get("kind") or "proxy")
    old = panel.db.get("sites", site_id) if site_id else None
    try:
        domains = [check_domain(d) for d in _lines(form.get("domains"))]
        if not domains:
            raise ValueError("podaj przynajmniej jedną domenę")
        for other in panel.db.all("sites"):
            if other["id"] != site_id and set(other["domains"]) & set(domains):
                raise ValueError(f"domena {', '.join(set(other['domains']) & set(domains))} jest już używana przez host #{other['id']}")
        values: dict[str, Any] = {"kind": kind, "domains": domains}
        if kind == "proxy":
            values["target"] = _target_from(form)
            values["locations"] = _locations_from(str(form.get("locations") or ""))
        elif kind == "redirect":
            values["redirect"] = Redirect(
                scheme=str(form.get("redirect_scheme") or "auto"), domain=str(form.get("redirect_domain") or ""),
                code=int(form.get("redirect_code") or 302), preserve_path=bool(form.get("preserve_path")),
            ).model_dump()  # fmt: skip
        for flag in FLAGS:
            values[flag] = int(bool(form.get(flag)))
        values["max_body_mb"] = max(0, min(int(form.get("max_body_mb") or 100), 100_000))
        values["access_list_id"] = int(form.get("access_list_id") or 0) or None
        values["ingress"] = str(form.get("ingress") or "public") if str(form.get("ingress") or "public") in ("public", "cloudflare") else "public"
        if has(user, Permission.ALLOW_PROXY_ADVANCED):
            values["advanced"] = str(form.get("advanced") or "").replace("\r\n", "\n")
        elif old is None:
            values["advanced"] = ""
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)

    cert_choice = str(form.get("cert") or "")
    new_cert = None
    if cert_choice == "new":
        new_cert = certs_svc.create(panel, domains)
        values["cert_id"] = new_cert
    elif cert_choice.isdigit():
        values["cert_id"] = int(cert_choice)
    else:
        values["cert_id"] = None

    site_id, result = await save_and_push(panel, "sites", site_id, values)
    message, level = proxy_state.push_message(result)
    site = panel.db.get("sites", site_id)
    if site["dns"]:
        try:
            await dns.ensure_records(panel, site)
        except (dns.DnsError, httpx.HTTPError) as exc:
            message, level = f"{message}\nDNS: {exc}", "warn"
    if new_cert:
        job = panel.jobs.start("cert", f"Certyfikat {', '.join(domains)}", lambda job: certs_svc.issue(panel, job, new_cert))
        return redirect(request, f"/jobs/{job.id}", "Zapisano host — wystawiam certyfikat")
    return redirect(request, f"/proxy/{SLUG_OF[kind]}", message, level)


@router.post("/proxy/hosts/{site_id}/toggle")
async def toggle_site(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, "Nie ma takiego hosta")
    _, result = await save_and_push(panel, "sites", site_id, {"enabled": 0 if site["enabled"] else 1})
    message, level = proxy_state.push_message(result)
    return redirect(request, request.headers.get("hx-current-url") or "/proxy/hosts", message, level)


@router.post("/proxy/hosts/{site_id}/delete")
async def delete_site(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, "Nie ma takiego hosta")
    if site.get("app_id"):
        raise LookupError("To host aplikacji — usuń aplikację albo jej domeny w ustawieniach aplikacji")
    await dns.remove_records(panel, site_id)
    panel.db.delete("sites", site_id)
    result = await proxy_state.push(panel)
    message, level = proxy_state.push_message(result)
    return redirect(request, f"/proxy/{SLUG_OF[site['kind']]}", f"Usunięto host. {message}", level)


# --- streamy -----------------------------------------------------------------------------------


@router.post("/proxy/streams/save")
async def save_stream(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    form = await request.form()
    stream_id = int(form.get("id") or 0) or None
    try:
        typed_host = str(form.get("upstream_host") or "").strip()
        node_id = None if typed_host else (int(form.get("node_id") or 0) or None)
        if not typed_host and not node_id:
            raise ValueError("wybierz węzeł albo wpisz adres docelowy")
        host = typed_host or "127.0.0.1"
        values = {
            "listen_port": int(form.get("listen_port") or 0), "node_id": node_id, "upstream_host": host,
            "upstream_port": int(form.get("upstream_port") or 0), "tcp": int(bool(form.get("tcp"))), "udp": int(bool(form.get("udp"))),
            "enabled": 1,
        }  # fmt: skip
        Stream(id=0, listen_port=values["listen_port"], upstream_host=host, upstream_port=values["upstream_port"])
        if values["listen_port"] in (80, 443, 51820, 51821, 2751):
            raise ValueError("ten port jest zajęty przez Deployra-WG / nginx")
        if not values["tcp"] and not values["udp"]:
            raise ValueError("zaznacz TCP lub UDP")
        for other in panel.db.all("streams"):
            if other["id"] != stream_id and other["listen_port"] == values["listen_port"]:
                raise ValueError("ten port nasłuchu jest już używany")
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    _, result = await save_and_push(panel, "streams", stream_id, values)
    message, level = proxy_state.push_message(result)
    return redirect(request, "/proxy/streams", message, level)


@router.post("/proxy/streams/{stream_id}/delete")
async def delete_stream(request: Request, stream_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    panel.db.delete("streams", stream_id)
    message, level = proxy_state.push_message(await proxy_state.push(panel))
    return redirect(request, "/proxy/streams", f"Usunięto stream. {message}", level)


# --- listy dostępu --------------------------------------------------------------------------------


@router.get("/proxy/access/new")
def new_acl(request: Request, user: dict = Depends(require(Permission.ALLOW_PROXY))):
    return render(request, "proxy/access_form.html", title="Nowa lista dostępu", acl={"users": [], "clients": []})


@router.get("/proxy/access/{acl_id}")
def edit_acl(request: Request, acl_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    acl = panel.db.get("access_lists", acl_id)
    if not acl:
        raise HTTPException(404, "Nie ma takiej listy")
    return render(request, "proxy/access_form.html", title=acl["name"], acl=acl)


@router.post("/proxy/access/save")
async def save_acl(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    form = await request.form()
    acl_id = int(form.get("id") or 0) or None
    old = panel.db.get("access_lists", acl_id) if acl_id else None
    old_users = {u["username"]: u["htpasswd"] for u in (old or {}).get("users") or []}
    try:
        users = []
        for line in _lines_keep_commas(str(form.get("users") or "")):
            username, sep, password = line.partition(":")
            username = username.strip()
            if not sep or not password.strip():
                if username in old_users:  # sam login = zostaw stare hasło
                    users.append(AccessUser(username=username, htpasswd=old_users[username]).model_dump())
                    continue
                raise ValueError(f"użytkownik „{line}” — oczekiwano login:hasło")
            users.append(AccessUser(username=username, htpasswd=apr1(password.strip())).model_dump())
        clients = []
        for line in _lines_keep_commas(str(form.get("clients") or "")):
            directive, _, address = line.partition(" ")
            if directive not in ("allow", "deny"):
                directive, address = "allow", line
            clients.append(AccessClient(address=address.strip(), directive=directive).model_dump())
        values = {"name": str(form.get("name") or "lista").strip()[:80], "satisfy_any": int(bool(form.get("satisfy_any"))),
                  "pass_auth": int(bool(form.get("pass_auth"))), "users": users, "clients": clients}  # fmt: skip
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    _, result = await save_and_push(panel, "access_lists", acl_id, values)
    message, level = proxy_state.push_message(result)
    return redirect(request, "/proxy/access", message, level)


@router.post("/proxy/access/{acl_id}/delete")
async def delete_acl(request: Request, acl_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    if panel.db.fetchone("SELECT id FROM sites WHERE access_list_id = ?", [acl_id]):
        raise LookupError("Lista jest używana przez hosty — najpierw je odepnij")
    panel.db.delete("access_lists", acl_id)
    await proxy_state.push(panel)
    return redirect(request, "/proxy/access", "Usunięto listę dostępu")


def _lines_keep_commas(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").splitlines() if line.strip() and not line.strip().startswith("#")]
