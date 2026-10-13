"""Ustawienia: ogólne, DNS/Cloudflare (z tunelem), użytkownicy, kopia zapasowa, strefa ryzyka."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from deployra_common.client import RemoteError
from deployra_common.passwords import hash_password

from ..context import Panel, get_panel
from ..db import now
from ..i18n import catalog, t
from ..deps import current_user, require
from ..permissions import LABELS, RISKY, Permission, encode, grantable, has, parse
from ..security import SESSION_COOKIE
from ..services import cloudflared, dns
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GENERAL_KEYS = ("panel_domain", "base_domain", "acme_email", "default_page", "default_page_target", "gravatar_default", "image_prefix", "language")


@router.get("/settings")
def general(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    return render(
        request, "settings/general.html", title=t("nav.settings"), tab="general",
        s=panel.db.settings(), db=panel.db.dialect,
        apps=panel.db.all("apps"), sites=panel.db.all("sites"),
    )


@router.post("/settings")
async def save_general(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    form = await request.form()
    for key in GENERAL_KEYS:
        value = str(form.get(key) or "").strip()
        if key == "base_domain":
            value = value.lower().removeprefix("*.").strip(".")
        if key == "language":
            value = catalog.supported(value) or ""
            panel.default_language = value or panel.config.language or None
        panel.db.set_setting(key, value)
    panel.db.set_setting("acme_staging", bool(form.get("acme_staging")))
    from ..services import proxy_state

    await proxy_state.push(panel)
    return toast(t("msg.settings_saved"))


# --- DNS i Cloudflare -------------------------------------------------------------------------


@router.get("/settings/dns")
def dns_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    s = panel.db.settings()
    return render(request, "settings/dns.html", title=t("settings.tab_dns"), tab="dns", s=s, has_token=bool(s.get("cloudflare_token")),
                  tunnel=cloudflared.state(panel))  # fmt: skip


@router.post("/settings/dns")
async def save_dns(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    form = await request.form()
    token = str(form.get("cloudflare_token") or "").strip()
    if token:
        panel.db.set_setting("cloudflare_token", panel.sealer.seal(token))
    if form.get("clear_token"):
        panel.db.set_setting("cloudflare_token", None)
    panel.db.set_setting("cloudflare_proxied", bool(form.get("cloudflare_proxied")))
    public_ip = str(form.get("public_ip") or "").strip()
    if public_ip:
        import ipaddress

        try:
            ipaddress.ip_address(public_ip)
        except ValueError:
            return toast(t("msg.bad_ip"), "err", status=400)
    panel.db.set_setting("public_ip", public_ip)
    return redirect(request, "/settings/dns", t("msg.dns_saved"))


@router.post("/settings/dns/verify")
async def verify_token(panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    cf = dns.client(panel)
    if not cf:
        return toast(t("msg.no_cf_token"), "warn")
    try:
        active = await cf.verify()
        zones = await cf.zones()
    except (dns.DnsError, httpx.HTTPError) as exc:
        return toast(t("msg.token_broken", error=exc), "err", status=400)
    finally:
        await cf.aclose()
    names = ", ".join(z["name"] for z in zones) or t("msg.no_zones")
    return toast(t("msg.token_active" if active else "msg.token_inactive", zones=names), "ok" if active else "warn")


@router.post("/settings/dns/tunnel")
async def enable_tunnel(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_TUNNEL))):
    try:
        await cloudflared.enable(panel)
    except (cloudflared.TunnelError, dns.DnsError, RemoteError, httpx.HTTPError) as exc:
        return toast(f"Cloudflare Tunnel: {getattr(exc, 'detail', exc)}", "err", status=400)  # nazwa własna + szczegóły błędu
    return redirect(request, "/settings/dns", t("msg.cf_tunnel_on"))


@router.post("/settings/dns/tunnel/disable")
async def disable_tunnel(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_TUNNEL))):
    await cloudflared.disable(panel)
    return redirect(request, "/settings/dns", t("msg.cf_tunnel_off"), "warn")


# --- użytkownicy ------------------------------------------------------------------------------


@router.get("/settings/users")
def users_page(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("users")
    return render(request, "settings/users.html", title=t("nav.users"), tab="users", users=rows, labels=LABELS, risky=RISKY,
                  parse=parse, grantable=grantable)  # fmt: skip


@router.post("/settings/users/save")
async def save_user(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    form = await request.form()
    user_id = int(form.get("id") or 0) or None
    target = panel.db.get("users", user_id) if user_id else None
    if user_id and not target:
        raise HTTPException(404, t("msg.no_user"))
    if target and target["id"] != user["id"] and not has(user, Permission.ALLOW_MODIFYING_USERS):
        return toast(t("msg.no_perm_edit_users"), "err", status=403)
    if not target and not has(user, Permission.ALLOW_CREATING_USERS_SETTING):
        return toast(t("msg.no_perm_create_users"), "err", status=403)
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")
    if not EMAIL.match(email):
        return toast(t("msg.bad_email"), "err", status=400)
    clash = panel.db.fetchone("SELECT id FROM users WHERE email = ?", [email])
    if clash and (not target or clash["id"] != target["id"]):
        return toast(t("msg.user_exists"), "err", status=400)
    if not target and len(password) < 8:
        return toast(t("msg.password_short"), "err", status=400)
    if password and len(password) < 8:
        return toast(t("msg.password_short"), "err", status=400)
    # uprawnienia: można nadać tylko te, które samemu się ma (ryzykowne — tylko z ALLOW_RISKY_PERMISSIONS)
    requested = {p for p in Permission if form.get(f"perm_{p.name}")}
    current = parse(target["permissions"]) if target else set()
    final = set()
    for perm in Permission:
        if perm in requested and grantable(user, perm):
            final.add(perm)
        elif perm in current and not grantable(user, perm):
            final.add(perm)  # nie możesz odebrać czegoś, czego nie możesz nadać
    if target and target.get("undeletable"):
        final.add(Permission.FULL_ACCESS)
    values = {"email": email, "name": str(form.get("name") or "")[:60], "surname": str(form.get("surname") or "")[:60]}
    if target and target["id"] == user["id"]:
        final = current  # własnych uprawnień się nie zmienia
    # konto „tylko przypisane serwery” — nie dla siebie samego ani pierwszego administratora; bez pełnego dostępu
    scoped = form.get("scoped") == "1" and has(user, Permission.ALLOW_MODIFYING_USERS)
    if target and (target["id"] == user["id"] or target.get("undeletable")):
        scoped = bool(target.get("scoped"))
    if scoped:
        final.discard(Permission.FULL_ACCESS)
    values["scoped"] = int(scoped)
    values["permissions"] = encode(final)
    if password:
        values["password_hash"] = await run_in_threadpool(hash_password, password)
    if target:
        panel.db.update("users", target["id"], **values)
        if password and target["id"] != user["id"]:
            panel.sessions.destroy_for_user(target["id"])  # zmiana hasła wylogowuje inne sesje tego konta
    else:
        panel.db.insert("users", undeletable=0, created_at=now(), **values)
    return redirect(request, "/settings/users", t("msg.user_saved"))


@router.post("/settings/users/{user_id}/delete")
def delete_user(request: Request, user_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DELETING_USERS))):
    target = panel.db.get("users", user_id)
    if not target:
        raise HTTPException(404, t("msg.no_user"))
    if target.get("undeletable"):
        return toast(t("msg.first_admin"), "err", status=400)
    if target["id"] == user["id"]:
        return toast(t("msg.delete_self"), "err", status=400)
    panel.sessions.destroy_for_user(target["id"])
    panel.db.delete("users", target["id"])
    return redirect(request, "/settings/users", t("msg.user_deleted", email=target["email"]))


# --- przypisane serwery (konta w stylu Pterodactyla) -------------------------------------------------


def _target_user(panel: Panel, user_id: int) -> dict:
    target = panel.db.get("users", user_id)
    if not target:
        raise HTTPException(404, t("msg.no_user"))
    return target


@router.get("/settings/users/{user_id}/servers")
def user_servers(request: Request, user_id: int, panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_MODIFYING_USERS))):  # fmt: skip
    from ..services import access

    target = _target_user(panel, user_id)
    apps = panel.db.fetchall("SELECT a.id, a.name, a.slug, n.name AS node_name FROM apps a LEFT JOIN nodes n ON n.id = a.node_id ORDER BY a.name")
    nodes = panel.db.fetchall("SELECT id, name FROM nodes WHERE status = 'paired' ORDER BY name")
    return render(request, "settings/user_servers.html", title=t("servers.servers_of", email=target["email"]), tab="users", target=target,
                  grants=access.grants_for(panel, user_id), apps=apps, nodes=nodes, perms=access.ALL, names=access.NAMES,
                  app_only=access.APP_ONLY)  # fmt: skip


@router.post("/settings/users/{user_id}/servers")
async def add_user_server(request: Request, user_id: int, panel: Panel = Depends(get_panel),
                          user: dict = Depends(require(Permission.ALLOW_MODIFYING_USERS))):  # fmt: skip
    from ..services import access

    _target_user(panel, user_id)
    form = await request.form()
    kind = str(form.get("kind") or "app")
    perms = "".join(p for p in access.ALL if form.get(f"perm_{p}"))
    access.add(panel, user_id, kind, perms, app_id=int(form.get("app_id") or 0) or None, node_id=int(form.get("node_id") or 0) or None,
               container=str(form.get("container") or "").strip() or None)  # fmt: skip
    return redirect(request, f"/settings/users/{user_id}/servers", t("servers.added"))


@router.post("/settings/users/{user_id}/servers/{grant_id}")
async def update_user_server(request: Request, user_id: int, grant_id: int, panel: Panel = Depends(get_panel),
                             user: dict = Depends(require(Permission.ALLOW_MODIFYING_USERS))):  # fmt: skip
    from ..services import access

    row = panel.db.fetchone("SELECT * FROM grants WHERE id = ? AND user_id = ?", [grant_id, user_id])
    if not row:
        raise HTTPException(404, t("servers.not_found"))
    form = await request.form()
    perms = access.clean_perms("".join(p for p in access.ALL if form.get(f"perm_{p}")), row["kind"])
    panel.db.update("grants", grant_id, perms=perms)
    return toast(t("servers.saved"))


@router.post("/settings/users/{user_id}/servers/{grant_id}/delete")
def delete_user_server(request: Request, user_id: int, grant_id: int, panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_MODIFYING_USERS))):  # fmt: skip
    panel.db.execute("DELETE FROM grants WHERE id = ? AND user_id = ?", [grant_id, user_id])
    return redirect(request, f"/settings/users/{user_id}/servers", t("servers.removed"))


# --- kopia zapasowa -----------------------------------------------------------------------------

BACKUP_TABLES = ("settings", "access_lists", "certificates", "sites", "streams", "monitors")


@router.get("/settings/backup")
def backup_page(request: Request, user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    return render(request, "settings/backup.html", title=t("settings.tab_backup"), tab="backup")


@router.get("/settings/backup/export")
def export(secrets_included: bool = False, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    data = {"deployra": 1, "exported_at": now(), "tables": {}}
    for table in BACKUP_TABLES:
        rows = panel.db.fetchall(f"SELECT * FROM {table}")
        if table == "settings":
            rows = [r for r in rows if secrets_included or r["skey"] != "cloudflare_token"]
            for r in rows:
                value = json.loads(r["value"])
                if r["skey"] == "cloudflare_token" and value:
                    value = {"plain": panel.sealer.unseal(value)}
                r["value"] = value
        data["tables"][table] = rows
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="deployra-backup-{stamp}.json"'},
    )


@router.post("/settings/backup/import")
async def import_backup(request: Request, file: UploadFile, panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):  # fmt: skip
    try:
        data = json.loads(await file.read())
        assert data.get("deployra") == 1
    except (ValueError, AssertionError):
        return toast(t("msg.not_backup"), "err", status=400)
    restored = 0
    with panel.db.transaction():
        for row in data["tables"].get("settings", []):
            value = row["value"]
            if row["skey"] == "cloudflare_token" and isinstance(value, dict):
                value = panel.sealer.seal(value.get("plain"))
            panel.db.set_setting(row["skey"], value)
        for table in ("access_lists", "certificates", "sites", "streams"):
            panel.db.execute(f"DELETE FROM {table}")
            for row in data["tables"].get(table, []):
                panel.db.insert(table, **row)
                restored += 1
    from ..services import proxy_state

    result = await proxy_state.push(panel)
    message, level = proxy_state.push_message(result)
    return redirect(request, "/settings/backup", t("msg.restored", count=restored) + " " + message, level)


# --- strefa ryzyka ---------------------------------------------------------------------------------


@router.get("/settings/risky")
def risky_page(request: Request, user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    return render(request, "settings/risky.html", title=t("settings.tab_risky"), tab="risky")


@router.post("/settings/risky/reset")
def factory_reset(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    """Fabryczny reset panelu: czyści bazę. Węzły i bramka zostają nietknięte (trzeba je potem zresetować i sparować)."""
    with panel.db.transaction():
        for table in ("monitor_checks", "monitor_hourly", "monitors", "dns_records", "apps", "sites", "streams", "access_lists",
                      "certificates", "nodes", "gateway", "notifications", "settings", "sessions", "users"):
            panel.db.execute(f"DELETE FROM {table}")
    panel._clients.clear()
    response = redirect(request, "/setup", t("msg.factory_reset"), "warn")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
