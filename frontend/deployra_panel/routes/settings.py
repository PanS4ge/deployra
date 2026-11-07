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
from deployra_common.passwords import hash_password, verify_password

from ..context import Panel, get_panel
from ..db import now
from ..i18n import catalog, t
from ..deps import current_user, require
from ..permissions import GROUPS, LABELS, RISKY, Permission, encode, grantable, has, parse
from ..security import SESSION_COOKIE
from ..services import cloudflared, db_backups, dns
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
def users_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    rows = panel.db.all("users")
    request.state.require_2fa = bool(panel.db.setting("require_2fa"))
    if not any(has(user, p) for p in USER_ADMIN):  # bez zarządzania kontami: tylko własne konto (zmiana hasła, imienia)
        rows = [r for r in rows if r["id"] == user["id"]]
    return render(request, "settings/users.html", title=t("nav.users"), tab="users", users=rows, labels=LABELS, risky=RISKY,
                  parse=parse, grantable=grantable, groups=GROUPS)  # fmt: skip


USER_ADMIN = (Permission.ALLOW_CREATING_USERS_SETTING, Permission.ALLOW_MODIFYING_USERS, Permission.ALLOW_DELETING_USERS)


def _admin_account(row: dict) -> bool:
    """Pierwsze konto albo konto z pełnym dostępem."""
    return bool(row.get("undeletable")) or Permission.FULL_ACCESS in parse(row.get("permissions"))


@router.post("/settings/users/save")
async def save_user(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    form = await request.form()
    user_id = int(form.get("id") or 0) or None
    target = panel.db.get("users", user_id) if user_id else None
    if user_id and not target:
        raise HTTPException(404, t("msg.no_user"))
    if target and target["id"] != user["id"] and not has(user, Permission.ALLOW_MODIFYING_USERS):
        return toast(t("msg.no_perm_edit_users"), "err", status=403)
    if target and target["id"] != user["id"] and _admin_account(target) and not has(user, Permission.FULL_ACCESS):
        # hasło / e-mail administratora = przejęcie pełnego dostępu
        return toast(t("msg.no_perm_edit_admin"), "err", status=403)
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
    if target and target["id"] == user["id"] and (password or email != target["email"]):
        current = str(form.get("current_password") or "")  # sesja na cudzym komputerze nie wystarczy do zmiany danych logowania
        if not await run_in_threadpool(verify_password, current, target["password_hash"]):
            return toast(t("msg.bad_current_password"), "err", status=403)
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
        elif password:  # własne hasło: zostaje tylko ta sesja
            panel.sessions.destroy_others(target["id"], user.get("sid"))
    else:
        panel.db.insert("users", undeletable=0, created_at=now(), **values)
    return redirect(request, "/settings/users", t("msg.user_saved"))


@router.post("/settings/users/{user_id}/2fa/reset")
def reset_user_2fa(request: Request, user_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.FULL_ACCESS))):
    """Zgubiony telefon: pełny dostęp wyłącza 2FA innego konta (i kończy jego sesje)."""
    target = panel.db.get("users", user_id)
    if not target:
        raise HTTPException(404, t("msg.no_user"))
    panel.db.update("users", user_id, totp_secret=None, totp_step=None, backup_codes=None)
    panel.sessions.destroy_for_user(user_id)
    return redirect(request, "/settings/users", t("account.2fa_reset", email=target["email"]))


@router.post("/settings/users/require-2fa")
def require_2fa(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    value = not bool(panel.db.setting("require_2fa"))
    if value and not user.get("totp_secret"):
        return toast(t("account.enable_yours_first"), "err", status=400)
    panel.db.set_setting("require_2fa", value)
    return redirect(request, "/settings/users", t("account.require_on") if value else t("account.require_off"))


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
def backup_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    config = panel.db.setting("backup_config") or {}
    db_schedules = db_backups.schedules(panel)
    running = [s["id"] for s in db_schedules if db_backups.is_running(s["id"])]
    return render(request, "settings/backup.html", title=t("settings.tab_backup"), tab="backup", config=config,
                  db_schedules=db_schedules, db_running=running)  # fmt: skip


@router.post("/settings/backup/storage")
async def save_backup_storage(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    form = await request.form()
    curr = panel.db.setting("backup_config") or {}
    target = str(form.get("target") or "local")
    retention = int(form.get("retention") or 10)
    auto_hours = max(0, min(int(form.get("auto_hours") or 0), 24 * 30))
    secrets_included = bool(form.get("secrets_included"))

    s3_secret = str(form.get("s3_secret_key") or "").strip()
    ftp_pwd = str(form.get("ftp_password") or "").strip()
    sftp_pwd = str(form.get("sftp_password") or "").strip()

    conf = {
        **curr,
        "target": target,
        "retention": retention,
        "auto_hours": auto_hours,
        "secrets_included": secrets_included,
        "local_path": str(form.get("local_path") or "./backups").strip(),
        "s3_endpoint": str(form.get("s3_endpoint") or "https://s3.amazonaws.com").strip(),
        "s3_bucket": str(form.get("s3_bucket") or "").strip(),
        "s3_access_key": str(form.get("s3_access_key") or "").strip(),
        "s3_region": str(form.get("s3_region") or "us-east-1").strip(),
        "s3_prefix": str(form.get("s3_prefix") or "").strip(),
        "ftp_host": str(form.get("ftp_host") or "").strip(),
        "ftp_port": int(form.get("ftp_port") or 21),
        "ftp_user": str(form.get("ftp_user") or "").strip(),
        "ftp_path": str(form.get("ftp_path") or "/").strip(),
        "ftp_tls": bool(form.get("ftp_tls")),
        "sftp_host": str(form.get("sftp_host") or "").strip(),
        "sftp_port": int(form.get("sftp_port") or 22),
        "sftp_user": str(form.get("sftp_user") or "root").strip(),
        "sftp_path": str(form.get("sftp_path") or "/backups").strip(),
    }
    if s3_secret:
        conf["s3_secret_key"] = panel.sealer.seal(s3_secret)
    if ftp_pwd:
        conf["ftp_password"] = panel.sealer.seal(ftp_pwd)
    if sftp_pwd:
        conf["sftp_password"] = panel.sealer.seal(sftp_pwd)

    panel.db.set_setting("backup_config", conf)
    return redirect(request, "/settings/backup", t("msg.settings_saved"))


@router.post("/settings/backup/storage/test")
async def test_backup_storage(panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    from ..services import backup_storage

    try:
        driver = backup_storage.get_driver(panel)
        msg = driver.test()
        return toast(msg, "ok")
    except Exception as exc:
        return toast(f"Błąd połączenia: {exc}", "err", status=400)


@router.post("/settings/backup/storage/run")
async def run_backup_now(panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):
    from ..services import backup_storage

    res = backup_storage.run_backup(panel)
    if not res.get("ok"):
        from ..services import watch

        await watch.backup_failed(panel, res.get("error") or "?")
    if res.get("ok"):
        return toast(f"Kopia zapasowa wykonana pomyślnie ({res.get('filename')})", "ok")
    return toast(f"Błąd kopii zapasowej: {res.get('error')}", "err", status=500)


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
    raw_nginx = has(user, Permission.ALLOW_PROXY_ADVANCED)  # surowy nginx z pliku kopii tylko dla tych, którzy mogą go pisać
    with panel.db.transaction():
        for row in data["tables"].get("settings", []):
            value = row["value"]
            if row["skey"] == "cloudflare_token" and isinstance(value, dict):
                value = panel.sealer.seal(value.get("plain"))
            panel.db.set_setting(row["skey"], value)
        for table in ("access_lists", "certificates", "sites", "streams"):
            panel.db.execute(f"DELETE FROM {table}")
            for row in data["tables"].get(table, []):
                if table == "sites" and not raw_nginx:
                    row = {**row, "advanced": "", "locations": [{**loc, "advanced": ""} for loc in row.get("locations") or [] if isinstance(loc, dict)]}
                panel.db.insert(table, **row)
                restored += 1
    from ..services import proxy_state

    result = await proxy_state.push(panel)
    message, level = proxy_state.push_message(result)
    return redirect(request, "/settings/backup", t("msg.restored", count=restored) + " " + message, level)


# --- rejestry Docker -------------------------------------------------------------------------------


@router.get("/settings/registries")
def registries_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    from ..services import registries

    items = registries.list_registries(panel)
    return render(request, "settings/registries.html", title=t("settings.tab_registries"), tab="registries", items=items)


@router.post("/settings/registries")
async def add_registry_route(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    from ..services import registries

    form = await request.form()
    name = str(form.get("name") or "").strip()
    url = str(form.get("url") or "").strip()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "").strip()
    try:
        registries.add_registry(panel, name, url, username, password)
        return redirect(request, "/settings/registries", t("msg.registry_added"))
    except ValueError as exc:
        return toast(str(exc), "err", status=400)


@router.post("/settings/registries/{reg_id}/delete")
def delete_registry_route(request: Request, reg_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    from ..services import registries

    registries.delete_registry(panel, reg_id)
    return redirect(request, "/settings/registries", t("msg.registry_deleted"))


# --- zmienne współdzielone ------------------------------------------------------------------------


@router.get("/settings/env-groups")
def env_groups_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    from ..services import env_groups
    from .apps import editor_state

    items = []
    for group in env_groups.list_groups(panel):
        full = env_groups.get(panel, group["id"])
        items.append({**group, "enved": editor_state(f"eg-{group['id']}", full["env"], full["scopes"])})
    return render(request, "settings/env_groups.html", title=t("settings.tab_env_groups"), tab="env_groups", items=items,
                  enved_new=editor_state("eg-new", {}, {}))  # fmt: skip


async def _save_env_group(request: Request, panel: Panel, group_id: int | None):
    from ..services import env_groups
    from .apps import env_from_form

    form = await request.form()
    try:
        env, scopes = env_from_form(form)
        env_groups.save(panel, group_id, str(form.get("name") or ""), env, scopes)
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    return redirect(request, "/settings/env-groups", t("envg.msg.saved"))


@router.post("/settings/env-groups")
async def create_env_group(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    return await _save_env_group(request, panel, None)


@router.post("/settings/env-groups/{group_id}")
async def update_env_group(group_id: int, request: Request, panel: Panel = Depends(get_panel),
                           user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    return await _save_env_group(request, panel, group_id)


@router.post("/settings/env-groups/{group_id}/delete")
def delete_env_group(request: Request, group_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    from ..services import env_groups

    env_groups.delete(panel, group_id)
    return redirect(request, "/settings/env-groups", t("envg.msg.deleted"))


# --- Serwer MCP & Tokeny API ----------------------------------------------------------------------


_FRESH_TOKENS: dict[str, tuple[str, float]] = {}  # sesja → (nowy token, do kiedy pokazać); token nie trafia do adresu


@router.get("/settings/mcp")
def mcp_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    import time

    from ..services import tokens
    from ..services.mcp_server import registry

    fresh = _FRESH_TOKENS.pop(user.get("sid") or "", None)
    new_token = fresh[0] if fresh and fresh[1] > time.time() else ""
    everyone = has(user, Permission.FULL_ACCESS)  # pełny dostęp widzi i usuwa tokeny wszystkich kont
    token_list = tokens.list_tokens(panel, None if everyone else user["id"])
    base_url = str(request.base_url).rstrip("/")
    tools = registry.list_tools(user)
    return render(
        request, "settings/mcp.html", title=t("settings.tab_mcp"), tab="mcp",
        tokens=token_list, new_token=new_token, base_url=base_url, tools=tools, now_ts=time.time(),
    )


@router.post("/settings/mcp/tokens")
async def create_mcp_token_route(request: Request, panel: Panel = Depends(get_panel),
                                 user: dict = Depends(require(Permission.ALLOW_SETTINGS, Permission.ALLOW_MCP))):  # fmt: skip
    import time

    from ..services import tokens

    form = await request.form()
    name = str(form.get("name") or "Agent AI").strip()[:60]
    days = int(form.get("days") or 0) if str(form.get("days") or "0").isdigit() else 90
    raw_token, _ = tokens.create_token(panel, name, user["id"], days=min(days, 3650) or None)
    if user.get("sid"):
        _FRESH_TOKENS[user["sid"]] = (raw_token, time.time() + 300)
    return redirect(request, "/settings/mcp", t("mcp.token_created"))


@router.post("/settings/mcp/tokens/{token_id}/delete")
def delete_mcp_token_route(request: Request, token_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    from ..services import tokens

    if not tokens.delete_token(panel, token_id, None if has(user, Permission.FULL_ACCESS) else user["id"]):
        return toast(t("mcp.token_not_yours"), "err", status=404)
    return redirect(request, "/settings/mcp", t("mcp.token_deleted"))


# --- dziennik zdarzeń ---------------------------------------------------------------------------------


@router.get("/settings/audit")
def audit_page(request: Request, email: str = "", action: str = "", q: str = "", days: int = 30, page: int = 1,
               panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):  # fmt: skip
    from ..services import audit

    days, page = max(1, min(days, 3650)), max(1, page)
    rows = audit.listing(panel, email=email, action=action, query=q.strip()[:100], days=days, limit=101, offset=(page - 1) * 100)
    return render(request, "settings/audit.html", title=t("audit.title"), tab="audit", rows=rows[:100], more=len(rows) > 100,
                  page=page, f={"email": email, "action": action, "q": q, "days": days}, emails=audit.emails(panel),
                  actions=audit.actions(panel), keep=int(panel.db.setting("audit_days") or audit.DEFAULT_DAYS))  # fmt: skip


@router.get("/settings/audit.csv")
def audit_csv(email: str = "", action: str = "", q: str = "", days: int = 30, panel: Panel = Depends(get_panel),
              user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):  # fmt: skip
    from ..services import audit

    rows = audit.listing(panel, email=email, action=action, query=q.strip()[:100], days=max(1, min(days, 3650)), limit=50_000)
    return Response(audit.to_csv(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="deployra-audit.csv"'})  # fmt: skip


@router.post("/settings/audit/retention")
def audit_retention(request: Request, days: int = Form(90), panel: Panel = Depends(get_panel),
                    user: dict = Depends(require(Permission.ALLOW_RISKY_SETTINGS))):  # fmt: skip
    from ..services import audit

    panel.db.set_setting("audit_days", max(7, min(days, 3650)))
    audit.cleanup(panel)
    return redirect(request, "/settings/audit", t("audit.retention_saved"))


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
