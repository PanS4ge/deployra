"""Proxy (zamiennik Nginx Proxy Managera): hosty, przekierowania, 404, streamy, listy dostępu."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from deployra_common.client import RemoteError
from deployra_common.schemas import AccessClient, Redirect, Stream, Upstream, check_domain

from ..context import Panel, get_panel
from ..db import now
from ..i18n import t
from ..deps import current_user, require
from ..permissions import Permission, has
from ..services import certs as certs_svc
from ..services import dns, proxy_state
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
KIND_SLUG = {"hosts": "proxy", "redirects": "redirect", "dead": "dead"}
SLUG_OF = {v: k for k, v in KIND_SLUG.items()}
KIND_TITLE = {"proxy": "dashboard.proxy_hosts", "redirect": "proxy.tab_redirects", "dead": "proxy.tab_dead"}
FLAGS = ("ssl_forced", "http2", "http3", "hsts", "hsts_subdomains", "block_exploits", "websockets", "caching", "trust_forwarded_proto", "enabled", "dns")


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
        raise LookupError(t("msg.nginx_rejected", error=result.get("message")))
    return row_id, result


# --- listy ------------------------------------------------------------------------------------


@router.get("/proxy")
def proxy_index(request: Request):
    return redirect(request, "/proxy/hosts")


@router.get("/proxy/streams")
def streams(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("streams")
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    return render(request, "proxy/streams.html", title=t("msg.title_streams"), tab="streams", streams=rows, nodes=nodes,
                  node_list=list(nodes.values()))  # fmt: skip


@router.get("/proxy/access")
def access_lists(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("access_lists")
    used = {}
    for site in panel.db.all("sites"):
        if site.get("access_list_id"):
            used[site["access_list_id"]] = used.get(site["access_list_id"], 0) + 1
    from ..services import access_accounts

    counts = {r["acl_id"]: r["n"] for r in panel.db.fetchall("SELECT acl_id, COUNT(*) AS n FROM access_accounts GROUP BY acl_id")}
    modes = {r["id"]: access_accounts.mode_of(r) for r in rows}
    return render(request, "proxy/access.html", title=t("proxy.tab_access"), tab="access", lists=rows, used=used, counts=counts, modes=modes)


@router.get("/proxy/health")
async def health(request: Request, ids: str = "", panel: Panel = Depends(get_panel)):
    """Czy cele hostów proxy odpowiadają — bramka sprawdza je wszystkie naraz."""
    from ..services import proxy_health

    wanted = [int(i) for i in ids.split(",") if i.isdigit()][:500]
    rows = panel.db.fetchall(f"SELECT * FROM sites WHERE kind = 'proxy' AND enabled = 1 AND id IN ({','.join('?' for _ in wanted)})",
                             wanted) if wanted else []  # fmt: skip
    return render(request, "partials/health_oob.html", results=await proxy_health.check(panel, rows))


@router.post("/proxy/unresponsive/{action}")
async def unresponsive(request: Request, action: str, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    """Archiwizuje albo usuwa hosty proxy, których cel nie odpowiada (wynik ostatniego sprawdzenia bramki)."""
    from ..services import proxy_health

    if action not in ("archive", "delete"):
        raise HTTPException(404, t("msg.no_section"))
    rows = panel.db.fetchall("SELECT * FROM sites WHERE kind = 'proxy' AND enabled = 1 AND archived = 0")
    results = await proxy_health.check(panel, rows)
    down = [s for s in rows if s["id"] in results and results[s["id"]].get("status") is None]
    if action == "delete":
        down = [s for s in down if not s.get("app_id")]  # host aplikacji usuwa się razem z aplikacją
    for site in down:
        if action == "archive":
            panel.db.update("sites", site["id"], archived=1, enabled=0)
        else:
            await dns.remove_records(panel, site["id"])
            panel.db.delete("sites", site["id"])
    if not down:
        return toast(t("proxy.none_down"), "info")
    message, level = proxy_state.push_message(await proxy_state.push(panel))
    done = t("proxy.archived_n" if action == "archive" else "proxy.deleted_n", count=len(down))
    return redirect(request, "/proxy/hosts", f"{done} {message}", level if level != "ok" else ("ok" if action == "archive" else "warn"))


@router.get("/proxy/{kind_slug}")
async def sites(request: Request, kind_slug: str, archived: bool = False, panel: Panel = Depends(get_panel)):
    kind = KIND_SLUG.get(kind_slug)
    if not kind:
        raise HTTPException(404, t("msg.no_section"))
    rows = panel.db.fetchall("SELECT * FROM sites WHERE kind = ? AND archived = ? ORDER BY id", [kind, int(archived)])
    archived_count = panel.db.scalar("SELECT COUNT(*) FROM sites WHERE kind = ? AND archived = 1", [kind])
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    certs = {c["id"]: c for c in panel.db.all("certificates")}
    apps = {a["id"]: a for a in panel.db.all("apps")}
    hops = await proxy_state.passthrough(panel, rows, nodes) if kind == "proxy" else {}
    from ..services import proxy_health

    health = proxy_health.known(panel, [r for r in rows if r["enabled"]]) if kind == "proxy" else {}
    enabled = [r["id"] for r in rows if r["enabled"]]
    chunks = [enabled[i:i + proxy_health.CHUNK] for i in range(0, len(enabled), proxy_health.CHUNK)]
    return render(request, "proxy/sites.html", title=t(KIND_TITLE[kind]), tab=kind_slug, kind=kind, sites=rows, nodes=nodes,
                  certs=certs, apps=apps, hops=hops, archived=archived, archived_count=archived_count, health=health,
                  health_chunks=chunks)  # fmt: skip


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
        "gateways": panel.gateways(),
        "has_cf": bool(panel.db.setting("cloudflare_token")),
    }  # fmt: skip


@router.get("/proxy/hosts/new")
def new_site(request: Request, kind: str = "proxy", panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    if kind not in KIND_TITLE:
        kind = "proxy"
    return render(request, "proxy/site_form.html", title=t("msg.title_new", kind=t(KIND_TITLE[kind]).lower()), **_form_context(panel, None, kind))


@router.get("/proxy/hosts/{site_id}")
def edit_site(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, t("msg.no_host"))
    return render(request, "proxy/site_form.html", title=", ".join(site["domains"][:2]), **_form_context(panel, site, site["kind"]))


@router.get("/proxy/hosts/{site_id}/gateway-conf")
async def gateway_conf(request: Request, site_id: int, panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_PROXY_ADVANCED))):  # fmt: skip
    """Konfiguracja hosta wprost z dysku każdej bramki — widać, czy host w ogóle do niej trafił i czy nginx ją przyjął."""
    if not panel.db.get("sites", site_id):
        raise HTTPException(404, t("msg.no_host"))
    rows = []
    for gw in panel.gateways():
        client = panel.client_for_gateway(gw)
        if not client:
            continue
        row = {"name": proxy_state.gateway_label(gw), "role": gw.get("role")}
        try:
            data = await client.get(f"/api/proxy/sites/{site_id}")
            row.update(conf=data.get("conf"), last=data.get("last_apply") or {})
        except Exception as exc:  # noqa: BLE001 — stara wersja bramki albo brak połączenia
            row["error"] = str(exc) or type(exc).__name__
        rows.append(row)
    return render(request, "proxy/_gateway_conf.html", rows=rows)


@router.post("/proxy/hosts/{site_id}/route-test")
async def route_test(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    """Test trasy hop po hopie: bramka → cel (HTTP, jak nginx), panel → węzeł (API) i węzeł → cel (TCP) — widać, który
    odcinek łańcucha nie działa (np. bramka → hub → węzeł z WireGuardem → serwer WG)."""
    site = panel.db.get("sites", site_id)
    if not site or site["kind"] != "proxy":
        raise HTTPException(404, t("msg.no_host"))
    target = site.get("target") or {}
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    upstream = proxy_state.resolve_target(panel, target, nodes)
    hops: list[dict[str, Any]] = []
    node = nodes.get(int(target.get("via_node") or target.get("node_id") or 0))
    if node:
        try:
            await panel.node_client(node).get("/api/vitals")
            hops.append({"name": t("route.panel_node", node=node["name"]), "ok": True})
        except (RemoteError, httpx.HTTPError) as exc:
            hops.append({"name": t("route.panel_node", node=node["name"]), "ok": False, "detail": getattr(exc, "detail", None) or str(exc),
                         "hint": t("route.hint_node")})  # fmt: skip
        if target.get("type") == "address" and upstream:
            try:
                probe = await panel.node_client(node).post("/api/net/probe", json={"host": upstream.host, "port": upstream.port})
                hops.append({"name": t("route.node_target", node=node["name"], target=f"{upstream.host}:{upstream.port}"), "ok": probe.get("ok"),
                             "detail": probe.get("error") or (f"{probe.get('ms')} ms" + (f" · {probe['via']}" if probe.get("via") else "")),
                             "hint": None if probe.get("ok") else t("route.hint_target")})  # fmt: skip
            except (RemoteError, httpx.HTTPError) as exc:
                hops.append({"name": t("route.node_target", node=node["name"], target=f"{upstream.host}:{upstream.port}"), "ok": False,
                             "detail": getattr(exc, "detail", None) or str(exc), "hint": t("route.hint_old_node")})  # fmt: skip
    client = panel.gateway_client()
    if client and upstream:
        url = f"{upstream.scheme}://{'[' + upstream.host + ']' if ':' in upstream.host else upstream.host}:{upstream.port}{upstream.path or '/'}"
        try:
            check = await client.post("/api/check", json={"url": url, "host": site["domains"][0]})
            hops.append({"name": t("route.gateway_target", target=f"{upstream.host}:{upstream.port}"), "ok": check.get("ok"),
                         "detail": f"HTTP {check['status']} · {check.get('ms')} ms" if check.get("status") else check.get("error"),
                         "hint": None if check.get("ok") else t("route.hint_gateway")})  # fmt: skip
        except (RemoteError, httpx.HTTPError) as exc:
            hops.append({"name": t("route.gateway_target", target=url), "ok": False, "detail": getattr(exc, "detail", None) or str(exc)})
    return render(request, "proxy/_route_test.html", hops=hops)


def _target_from(form: Any, prefix: str = "") -> dict:
    kind = str(form.get(f"{prefix}target_type") or "node")
    scheme = str(form.get(f"{prefix}scheme") or "http")
    port = int(form.get(f"{prefix}port") or 0)
    path = str(form.get(f"{prefix}path") or "").strip()
    if kind == "node":
        node_id = int(form.get(f"{prefix}node_id") or 0)
        if not node_id:
            raise ValueError(t("msg.pick_node"))
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
            raise ValueError(t("msg.bad_location", line=line))
        up = Upstream(scheme=parsed.scheme, host=parsed.host, port=parsed.port or (443 if parsed.scheme == "https" else 80),
                      path=parsed.path if parsed.path != "/" else "")  # fmt: skip
        out.append({"path": path, "target": {"type": "address", **up.model_dump()}, "advanced": ""})
    return out


async def _route_through_node(panel: Panel, target: dict | None) -> str | None:
    """Host „adres za węzłem”: brakującą podsieć adresu dopisujemy węzłowi (hub, NAT na węźle, trasy bramki brzegowej)."""
    from ..services import sync

    if not target or target.get("type") != "address" or not target.get("via_node"):
        return None
    node = panel.node(int(target["via_node"]))
    if not node or node.get("transport") != "tunnel":
        return None
    subnet = sync.subnet_for(panel, node, target.get("host") or "")
    if not subnet:
        return None
    try:
        result = await sync.set_node_lan(panel, node, [*(node.get("lan_subnets") or []), subnet])
    except (LookupError, ValueError, RemoteError, httpx.HTTPError) as exc:
        return t("site.lan_failed", subnet=subnet, node=node["name"], error=getattr(exc, "detail", None) or exc)
    return t("site.lan_added", subnet=subnet, node=node["name"]) + ("" if result["node"] else " " + t("msg.subnets_saved_node_old"))


@router.post("/proxy/hosts/save")
async def save_site(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    form = await request.form()
    site_id = int(form.get("id") or 0) or None
    kind = str(form.get("kind") or "proxy")
    old = panel.db.get("sites", site_id) if site_id else None
    try:
        domains = [check_domain(d) for d in _lines(form.get("domains"))]
        if not domains:
            raise ValueError(t("msg.need_domain"))
        for other in panel.db.all("sites"):
            if other["id"] != site_id and set(other["domains"]) & set(domains):
                raise ValueError(t("msg.domain_taken", domain=", ".join(set(other["domains"]) & set(domains)), id=other["id"]))
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
        values["gateway_id"] = int(form.get("gateway_id") or 0) or None
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

    lan_note = await _route_through_node(panel, values.get("target") if kind == "proxy" else None)
    site_id, result = await save_and_push(panel, "sites", site_id, values)
    message, level = proxy_state.push_message(result)
    if lan_note:
        message = f"{message}\n{lan_note}"
    site = panel.db.get("sites", site_id)
    if site["dns"]:
        try:
            await dns.ensure_records(panel, site)
        except (dns.DnsError, httpx.HTTPError) as exc:
            message, level = f"{message}\nDNS: {exc}", "warn"
    if new_cert:
        job = panel.jobs.start("cert", t("jobs.title_cert", name=", ".join(domains)), lambda job: certs_svc.issue(panel, job, new_cert))
        return redirect(request, f"/jobs/{job.id}", t("msg.host_saved_cert"))
    return redirect(request, f"/proxy/{SLUG_OF[kind]}", message, level)


@router.post("/proxy/hosts/{site_id}/toggle")
async def toggle_site(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, t("msg.no_host"))
    _, result = await save_and_push(panel, "sites", site_id, {"enabled": 0 if site["enabled"] else 1})
    message, level = proxy_state.push_message(result)
    return redirect(request, request.headers.get("hx-current-url") or "/proxy/hosts", message, level)


@router.post("/proxy/hosts/{site_id}/archive")
async def archive_site(request: Request, site_id: int, restore: bool = False, panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    """Archiwum: host wyłączony i schowany z listy; przywrócenie włącza go z powrotem."""
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, t("msg.no_host"))
    _, result = await save_and_push(panel, "sites", site_id, {"archived": 0, "enabled": 1} if restore else {"archived": 1, "enabled": 0})
    message, level = proxy_state.push_message(result)
    done = t("proxy.restored" if restore else "proxy.archived_one", name=site["domains"][0] if site["domains"] else site_id)
    return redirect(request, request.headers.get("hx-current-url") or "/proxy/hosts", f"{done} {message}", level)


@router.post("/proxy/hosts/{site_id}/delete")
async def delete_site(request: Request, site_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    site = panel.db.get("sites", site_id)
    if not site:
        raise HTTPException(404, t("msg.no_host"))
    if site.get("app_id"):
        raise LookupError(t("msg.app_host"))
    await dns.remove_records(panel, site_id)
    panel.db.delete("sites", site_id)
    result = await proxy_state.push(panel)
    message, level = proxy_state.push_message(result)
    return redirect(request, f"/proxy/{SLUG_OF[site['kind']]}", t("msg.host_deleted") + " " + message, level)


# --- streamy -----------------------------------------------------------------------------------


@router.post("/proxy/streams/save")
async def save_stream(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    form = await request.form()
    stream_id = int(form.get("id") or 0) or None
    try:
        typed_host = str(form.get("upstream_host") or "").strip()
        node_id = None if typed_host else (int(form.get("node_id") or 0) or None)
        if not typed_host and not node_id:
            raise ValueError(t("msg.pick_node_or_address"))
        host = typed_host or "127.0.0.1"
        values = {
            "listen_port": int(form.get("listen_port") or 0), "node_id": node_id, "upstream_host": host,
            "upstream_port": int(form.get("upstream_port") or 0), "tcp": int(bool(form.get("tcp"))), "udp": int(bool(form.get("udp"))),
            "enabled": 1,
        }  # fmt: skip
        Stream(id=0, listen_port=values["listen_port"], upstream_host=host, upstream_port=values["upstream_port"])
        if values["listen_port"] in (80, 443, 51820, 51821, 2751):
            raise ValueError(t("msg.port_reserved"))
        if not values["tcp"] and not values["udp"]:
            raise ValueError(t("msg.pick_protocol"))
        for other in panel.db.all("streams"):
            if other["id"] != stream_id and other["listen_port"] == values["listen_port"]:
                raise ValueError(t("msg.port_used"))
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    _, result = await save_and_push(panel, "streams", stream_id, values)
    message, level = proxy_state.push_message(result)
    return redirect(request, "/proxy/streams", message, level)


@router.post("/proxy/streams/{stream_id}/delete")
async def delete_stream(request: Request, stream_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    panel.db.delete("streams", stream_id)
    message, level = proxy_state.push_message(await proxy_state.push(panel))
    return redirect(request, "/proxy/streams", t("msg.stream_deleted") + " " + message, level)


# --- listy dostępu --------------------------------------------------------------------------------


def _acl_page(request: Request, panel: Panel, acl: dict, title: str):
    import time

    from ..services import access_accounts

    return render(request, "proxy/access_form.html", title=title, acl=acl, acl_mode=access_accounts.mode_of(acl) if acl.get("id") else "none",
                  panel_users=_panel_users(panel), accounts=access_accounts.accounts(panel, acl["id"]) if acl.get("id") else [],
                  sessions=access_accounts.sessions_of(panel, acl["id"]) if acl.get("id") else [], now_ts=time.time())  # fmt: skip


@router.get("/proxy/access/new")
def new_acl(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    return _acl_page(request, panel, {"users": [], "clients": [], "login_users": [], "allow_panel": 1}, t("msg.title_new_acl"))


@router.get("/proxy/access/{acl_id}")
def edit_acl(request: Request, acl_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    from ..services import access_accounts

    acl = panel.db.get("access_lists", acl_id)
    if not acl:
        raise HTTPException(404, t("msg.no_list"))
    if acl.get("users"):  # stare hasła Basic → konta listy (zwykle robi to już start panelu)
        access_accounts.migrate_legacy(panel)
        acl = panel.db.get("access_lists", acl_id)
    return _acl_page(request, panel, acl, acl["name"])


def _panel_users(panel: Panel) -> list[dict[str, Any]]:
    return panel.db.fetchall("SELECT id, email FROM users ORDER BY email")


@router.post("/proxy/access/save")
async def save_acl(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    from ..services import access_accounts

    form = await request.form()
    acl_id = int(form.get("id") or 0) or None
    old = panel.db.get("access_lists", acl_id) if acl_id else None
    try:
        clients = []
        for line in _lines_keep_commas(str(form.get("clients") or "")):
            directive, _, address = line.partition(" ")
            if directive not in ("allow", "deny"):
                directive, address = "allow", line
            clients.append(AccessClient(address=address.strip(), directive=directive).model_dump())
        mode = str(form.get("login_mode") or ("deployra" if form.get("deployra_login") else "none"))
        if mode not in access_accounts.MODES:
            mode = "none"
        known = {u["id"] for u in panel.db.all("users")}
        allow_panel = bool(form.get("allow_panel")) if "login_mode" in form else True
        login_users = sorted({int(v) for v in form.getlist("login_users") if str(v).isdigit() and int(v) in known}) if mode == "deployra" and allow_panel else []
        try:
            hours = max(1, min(int(form.get("session_hours") or access_accounts.DEFAULT_SESSION_HOURS), 720))
        except ValueError:
            hours = access_accounts.DEFAULT_SESSION_HOURS
        values = {
            "name": str(form.get("name") or "lista").strip()[:80], "satisfy_any": int(bool(form.get("satisfy_any"))),
            "pass_auth": int(bool(form.get("pass_auth"))) if mode == "basic" else 0, "users": [], "clients": clients,
            "login_mode": mode, "deployra_login": int(mode == "deployra"), "login_users": login_users, "allow_panel": int(allow_panel),
            "require_totp": int(bool(form.get("require_totp"))) if mode == "deployra" else 0, "session_hours": hours,
        }  # fmt: skip
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    if old and old.get("users"):
        access_accounts.migrate_legacy(panel)
    acl_id, result = await save_and_push(panel, "access_lists", acl_id, values)
    if old and access_accounts.mode_of(old) != mode:  # zmiana trybu: dotychczasowe sesje logowania Deployra przestają działać
        access_accounts.revoke_acl(panel, acl_id)
    message, level = proxy_state.push_message(result)
    if old is None:  # nowa lista — od razu do edycji, żeby dodać konta
        return redirect(request, f"/proxy/access/{acl_id}", message, level)
    return redirect(request, "/proxy/access", message, level)


@router.post("/proxy/access/{acl_id}/delete")
async def delete_acl(request: Request, acl_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    from ..services import access_accounts

    if panel.db.fetchone("SELECT id FROM sites WHERE access_list_id = ?", [acl_id]):
        raise LookupError(t("msg.list_in_use"))
    access_accounts.revoke_acl(panel, acl_id)
    panel.db.execute("DELETE FROM access_accounts WHERE acl_id = ?", [acl_id])
    panel.db.delete("access_lists", acl_id)
    await proxy_state.push(panel)
    return redirect(request, "/proxy/access", t("msg.list_deleted"))


# --- konta listy dostępu ----------------------------------------------------------------------------


def _account(panel: Panel, acl_id: int, account_id: int | None = None) -> tuple[dict, dict | None]:
    acl = panel.db.get("access_lists", acl_id)
    if not acl:
        raise HTTPException(404, t("msg.no_list"))
    if account_id is None:
        return acl, None
    account = panel.db.get("access_accounts", account_id)
    if not account or account["acl_id"] != acl_id:
        raise HTTPException(404, t("access.no_account"))
    return acl, account


async def _accounts_changed(request: Request, panel: Panel, acl: dict, message: str):
    """Po zmianie kont: w trybie okienka nginx bramka dostaje nowy plik htpasswd."""
    from ..services import access_accounts

    level = "ok"
    if access_accounts.mode_of(acl) == "basic":
        pushed, level = proxy_state.push_message(await proxy_state.push(panel))
        message = f"{message} {pushed}" if level != "ok" else message
    return redirect(request, f"/proxy/access/{acl['id']}", message, level)


@router.post("/proxy/access/{acl_id}/accounts")
async def add_account(request: Request, acl_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    from ..services import access_accounts

    acl, _ = _account(panel, acl_id)
    form = await request.form()
    try:
        username = str(form.get("username") or "")
        await run_in_threadpool(access_accounts.create, panel, acl_id, username, str(form.get("password") or ""), str(form.get("name") or ""))
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    return await _accounts_changed(request, panel, acl, t("access.account_added", name=username.strip()))


@router.post("/proxy/access/{acl_id}/accounts/{account_id}/password")
async def account_password(request: Request, acl_id: int, account_id: int, panel: Panel = Depends(get_panel),
                           user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    from ..services import access_accounts

    acl, account = _account(panel, acl_id, account_id)
    form = await request.form()
    try:
        await run_in_threadpool(access_accounts.set_password, panel, account_id, str(form.get("password") or ""))
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    return await _accounts_changed(request, panel, acl, t("access.password_changed", name=account["username"]))


@router.post("/proxy/access/{acl_id}/accounts/{account_id}/toggle")
async def account_toggle(request: Request, acl_id: int, account_id: int, panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    from ..services import access_accounts

    acl, account = _account(panel, acl_id, account_id)
    access_accounts.set_enabled(panel, account_id, not account["enabled"])
    return await _accounts_changed(request, panel, acl, t("access.account_saved", name=account["username"]))


@router.post("/proxy/access/{acl_id}/accounts/{account_id}/delete")
async def account_delete(request: Request, acl_id: int, account_id: int, panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    from ..services import access_accounts

    acl, account = _account(panel, acl_id, account_id)
    access_accounts.delete(panel, account_id)
    return await _accounts_changed(request, panel, acl, t("access.account_deleted", name=account["username"]))


@router.post("/proxy/access/{acl_id}/accounts/{account_id}/totp")
def account_totp(request: Request, acl_id: int, account_id: int, panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    """Nowy sekret 2FA — pokazany raz (do przekazania osobie, która będzie się logować)."""
    from deployra_common import totp

    from ..services import access_accounts

    acl, account = _account(panel, acl_id, account_id)
    secret = totp.new_secret()
    access_accounts.set_totp(panel, account_id, secret)
    label = f"{account['username']}@{acl['name']}"
    response = render(request, "proxy/_totp_reveal.html", username=account["username"], secret=secret, uri=totp.uri(secret, label))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/proxy/access/{acl_id}/accounts/{account_id}/totp/off")
def account_totp_off(request: Request, acl_id: int, account_id: int, panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    from ..services import access_accounts

    _, account = _account(panel, acl_id, account_id)
    access_accounts.set_totp(panel, account_id, None)
    return redirect(request, f"/proxy/access/{acl_id}", t("access.totp_removed", name=account["username"]))


@router.post("/proxy/access/{acl_id}/sessions/revoke")
def revoke_sessions(request: Request, acl_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    from ..services import access_accounts

    _account(panel, acl_id)
    access_accounts.revoke_acl(panel, acl_id)
    return redirect(request, f"/proxy/access/{acl_id}", t("access.sessions_revoked"))


def _lines_keep_commas(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").splitlines() if line.strip() and not line.strip().startswith("#")]
