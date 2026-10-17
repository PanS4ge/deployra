"""Minecraft: serwery z pluginem DeployraBridge (aplikacje na węzłach i hostingi zewnętrzne)."""

from __future__ import annotations

import base64
import json
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from deployra_common import hmac_auth

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import minecraft as mc
from ..web import redirect, render, toast

public = APIRouter()
router = APIRouter(dependencies=[Depends(current_user)])
JAR = Path(__file__).resolve().parent.parent / "static" / "downloads" / "DeployraBridge.jar"
TABS = ("console", "players", "properties", "files", "papi", "settings")


def panel_url(request: Request, panel: Panel) -> str:
    domain = panel.db.setting("panel_domain")
    return f"https://{domain}" if domain else str(request.base_url).rstrip("/")


def _server(server_id: int, panel: Panel = Depends(get_panel)) -> dict:
    row = mc.server(panel, server_id)
    if not row:
        raise HTTPException(404, t("mc.not_found"))
    return row


def _bridge_error(exc: Exception) -> Response:
    if isinstance(exc, mc.Offline):
        return toast(t("mc.offline"), "err", status=409)
    return toast(t("mc.bridge_error", error=str(exc)), "err", status=502)


# --- plugin: parowanie i kanał poleceń (bez sesji, podpis HMAC) ---------------------------------------


@public.get("/minecraft/download/DeployraBridge.jar")
def download_plugin() -> FileResponse:
    return FileResponse(JAR, media_type="application/java-archive", filename="DeployraBridge.jar")


@public.post("/minecraft/bridge/pair")
async def bridge_pair(request: Request, panel: Panel = Depends(get_panel)):
    try:
        data = json.loads(await request.body() or b"{}")
    except ValueError:
        data = {}
    client_ip = request.client.host if request.client else "?"
    found = mc.pair(panel, str(data.get("code") or "")[:64], data, client_ip)
    if not found:
        return JSONResponse({"detail": "invalid or expired pairing code"}, status_code=403)
    return found


@public.post("/minecraft/bridge/poll")
async def bridge_poll(request: Request, panel: Panel = Depends(get_panel)):
    path = "/minecraft/bridge/poll"
    body = await request.body()
    if len(body) > 16 * 1024 * 1024:
        return JSONResponse({"detail": "too large"}, status_code=413)
    try:
        row = mc.verify(panel, path, body, request.headers)
        payload = json.loads(body or b"{}")
    except (hmac_auth.SignatureError, ValueError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    commands = await mc.poll(panel, row, payload if isinstance(payload, dict) else {})
    out = json.dumps({"commands": commands}).encode()
    nonce = request.headers.get(hmac_auth.H_NONCE, "")
    return Response(out, media_type="application/json", headers=mc.sign_response(mc.secret_of(panel, row), f"{path}#{nonce}", out))


# --- lista i dodawanie ---------------------------------------------------------------------------------


@router.get("/minecraft")
def list_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):
    mc.import_legacy(panel)
    rows = mc.servers(panel)
    apps = {a["id"]: a for a in panel.db.fetchall("SELECT id, name, slug, status FROM apps")}
    items = [{"row": r, "online": mc.online(r), "info": mc.info(r), "app": apps.get(r.get("app_id"))} for r in rows]
    return render(request, "minecraft/list.html", title="Minecraft", items=items)


@router.post("/minecraft/add")
async def add_server(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return toast(t("common.required_field"), "err", status=400)
    app_id = int(form.get("app_id") or 0) or None
    if app_id and not panel.db.get("apps", app_id):
        return toast(t("msg.bad_data"), "err", status=400)
    server_id, _ = mc.create(panel, name, app_id)
    return redirect(request, f"/minecraft/{server_id}?tab=settings")


# --- szczegóły -------------------------------------------------------------------------------------------


@router.get("/minecraft/{server_id}")
def detail_page(request: Request, tab: str = "console", row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    tab = tab if tab in TABS else "console"
    from ..permissions import has

    app = panel.db.get("apps", row["app_id"]) if row.get("app_id") else None
    manager = has(user, Permission.ALLOW_MODIFY_DOCKERS)  # kod parowania pozwala podpiąć dowolny serwer — tylko dla zarządzających
    ftp = mc.get_ftp(panel, row) if manager else None
    ftp = {k: v for k, v in ftp.items() if k != "password"} if ftp else None
    return render(request, "minecraft/detail.html", title=row["name"], s=row, tab=tab, online=mc.online(row), info=mc.info(row), app=app,
                  code=mc.pending_code(panel, row) if manager else None, panel_url=panel_url(request, panel), macros=mc.macros(row), ftp=ftp,
                  manager=manager)  # fmt: skip


@router.get("/minecraft/{server_id}/status")
def status_badge(request: Request, row: dict = Depends(_server), user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):
    return render(request, "minecraft/_status.html", s=row, online=mc.online(row), info=mc.info(row))


@router.post("/minecraft/{server_id}/command")
async def command(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                  user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    form = await request.form()
    try:
        line = mc.clean_command(str(form.get("command") or ""))
    except ValueError:
        return toast(t("mc.bad_command"), "err", status=400)
    try:
        data = await mc.call(panel, row, "command", {"command": line})
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    return render(request, "minecraft/_lines.html", lines=[f"> {line}", *[str(x) for x in data.get("output") or []]], oob=True)


@router.get("/minecraft/{server_id}/logs")
async def logs(request: Request, since: int = 0, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
               user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    try:
        data = await mc.call(panel, row, "logs", {"since": since}, timeout=10)
    except (mc.Offline, mc.BridgeError):
        return render(request, "minecraft/_poller.html", s=row, since=since, lines=[])
    return render(request, "minecraft/_poller.html", s=row, since=int(data.get("next") or since), lines=data.get("lines") or [])


@router.get("/minecraft/{server_id}/players")
async def players(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                  user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    error = None
    try:
        data = await mc.call(panel, row, "players")
    except (mc.Offline, mc.BridgeError) as exc:
        data, error = {}, t("mc.offline") if isinstance(exc, mc.Offline) else str(exc)
    return render(request, "minecraft/_players.html", s=row, players=data.get("players") or [], error=error)


@router.post("/minecraft/{server_id}/player")
async def player_action(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    form = await request.form()
    try:
        op, args = mc.player_command(str(form.get("action") or ""), str(form.get("name") or ""), str(form.get("reason") or ""))
    except ValueError:
        return toast(t("mc.bad_player"), "err", status=400)
    try:
        data = await mc.call(panel, row, op, args)
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    return toast(str(data.get("message") or t("mc.done")), extra={"mc-players": True})


@router.get("/minecraft/{server_id}/properties")
async def properties(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES))):  # fmt: skip
    try:
        props = (await mc.call(panel, row, "properties.get")).get("properties") or {}
    except (mc.Offline, mc.BridgeError) as exc:
        return render(request, "minecraft/_properties.html", s=row, error=t("mc.offline") if isinstance(exc, mc.Offline) else str(exc))
    known = dict(mc.PROPERTIES)
    return render(request, "minecraft/_properties.html", s=row, fields=mc.PROPERTIES, props=props,
                  others=sorted(k for k in props if k not in known), error=None)  # fmt: skip


@router.post("/minecraft/{server_id}/properties")
async def save_properties(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                          user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE))):  # fmt: skip
    form = await request.form()
    changes = {}
    for key in {k for k in form.keys() if k.startswith("p:")}:
        values = form.getlist(key)  # pole bool: ukryte „false” + zaznaczone „true” — liczy się ostatnie
        value = str(values[-1] if values else "").replace("\r", "").replace("\n", " ")
        changes[key[2:]] = value[:400]
    try:
        await mc.call(panel, row, "properties.set", {"properties": changes})
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    return toast(t("mc.properties_saved"))


# --- pliki: plugin, a gdy serwer jest wyłączony — FTP ------------------------------------------------------


def _files_url(server_id: int, path: str) -> str:
    return f"/minecraft/{server_id}/files?{urlencode({'path': path})}"


async def _files(panel: Panel, row: dict, op: str, args: dict) -> dict:
    """Operacja na plikach przez plugin, z FTP jako zapasem (serwer wyłączony)."""
    if mc.online(row) and row.get("secret"):
        return await mc.call(panel, row, f"files.{op}", args, timeout=60)
    ftp = mc.get_ftp(panel, row)
    if not ftp:
        raise mc.Offline
    path = mc.clean_path(args.get("path") or "/")
    if op == "list":
        return {"items": await mc.ftp_call(ftp, "list", path), "via": "ftp"}
    if op == "read":
        return {"data_b64": base64.b64encode(await mc.ftp_call(ftp, "read", path)).decode()}
    if op == "write":
        await mc.ftp_call(ftp, "write", path, base64.b64decode(args["data_b64"]))
    elif op == "delete":
        await mc.ftp_call(ftp, "delete", path, bool(args.get("is_dir")))
    elif op == "mkdir":
        await mc.ftp_call(ftp, "mkdir", path)
    return {}


@router.get("/minecraft/{server_id}/files")
async def files(request: Request, path: str = "/", row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES))):  # fmt: skip
    path = mc.clean_path(path)
    error, data = None, {}
    try:
        data = await _files(panel, row, "list", {"path": path})
    except mc.Offline:
        error = t("mc.files_offline")
    except Exception as exc:  # noqa: BLE001 — błąd pluginu albo FTP pokazujemy w tabeli
        error = str(exc)[:300]
    crumbs, acc = [], ""
    for part in [x for x in path.strip("/").split("/") if x]:
        acc += "/" + part
        crumbs.append({"name": part, "url": _files_url(row["id"], acc)})
    items = []
    for item in data.get("items") or []:
        item_path = mc.clean_path(posixpath.join(path, str(item.get("name") or "")))
        items.append({**item, "path": item_path, "url": _files_url(row["id"], item_path) if item.get("is_dir") else None,
                      "download": None if item.get("is_dir") else f"/minecraft/{row['id']}/files/download?path={quote(item_path)}"})  # fmt: skip
    parent = None if path == "/" else (path.rstrip("/").rpartition("/")[0] or "/")
    return render(request, "minecraft/_files.html", s=row, cur_path=path, items=items, crumbs=crumbs, error=error, via=data.get("via"),
                  self_url=_files_url(row["id"], path), root_url=_files_url(row["id"], "/"),
                  parent_url=_files_url(row["id"], parent) if parent else None)  # fmt: skip


@router.get("/minecraft/{server_id}/files/read")
async def files_read(path: str, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES))):  # fmt: skip
    try:
        raw = base64.b64decode((await _files(panel, row, "read", {"path": mc.clean_path(path)})).get("data_b64") or "")
    except mc.Offline as exc:
        raise HTTPException(409, t("mc.files_offline")) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)[:300]) from exc
    binary = b"\x00" in raw[:4096]
    return {"path": path, "size": len(raw), "is_binary": binary, "truncated": False, "content": "" if binary else raw.decode("utf-8", errors="replace")}


@router.get("/minecraft/{server_id}/files/download")
async def files_download(path: str, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES))):  # fmt: skip
    try:
        raw = base64.b64decode((await _files(panel, row, "read", {"path": mc.clean_path(path)})).get("data_b64") or "")
    except mc.Offline as exc:
        raise HTTPException(409, t("mc.files_offline")) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)[:300]) from exc
    name = posixpath.basename(path) or "file"
    return Response(raw, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


async def _write(panel: Panel, row: dict, path: str, data: bytes) -> Response | None:
    if len(data) > mc.FILE_LIMIT:
        return toast(t("mc.file_too_big"), "err", status=413)
    try:
        await _files(panel, row, "write", {"path": mc.clean_path(path), "data_b64": base64.b64encode(data).decode()})
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    except Exception as exc:  # noqa: BLE001 — FTP
        return toast(str(exc)[:200], "err", status=400)
    return None


@router.post("/minecraft/{server_id}/files/write")
async def files_write(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE))):  # fmt: skip
    form = await request.form()
    error = await _write(panel, row, str(form.get("path") or ""), str(form.get("content") or "").encode())
    return error or toast(t("containers.file_saved"))


@router.post("/minecraft/{server_id}/files/upload")
async def files_upload(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE))):  # fmt: skip
    form = await request.form()
    directory = mc.clean_path(str(form.get("dir_path") or "/"))
    count = 0
    for upload in form.getlist("files"):
        name = posixpath.basename(str(getattr(upload, "filename", "") or "").replace("\\", "/"))
        if not name or name in (".", ".."):
            continue
        error = await _write(panel, row, posixpath.join(directory, name), await upload.read(mc.FILE_LIMIT + 1))
        if error:
            return error
        count += 1
    return redirect(request, f"/minecraft/{row['id']}?{urlencode({'tab': 'files', 'path': directory})}", t("containers.files_uploaded", count=count))


@router.post("/minecraft/{server_id}/files/delete")
async def files_delete(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE))):  # fmt: skip
    form = await request.form()
    path = mc.clean_path(str(form.get("path") or ""))
    if path == "/":
        return toast(t("mc.bad_path"), "err", status=400)
    try:
        await _files(panel, row, "delete", {"path": path, "is_dir": form.get("is_dir") == "1"})
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    except Exception as exc:  # noqa: BLE001
        return toast(str(exc)[:200], "err", status=400)
    return toast(t("containers.file_deleted", path=path), extra={"mc-files": True})


@router.post("/minecraft/{server_id}/files/mkdir")
async def files_mkdir(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE))):  # fmt: skip
    form = await request.form()
    name = posixpath.basename(str(form.get("name") or "").strip().strip("/"))
    if not name or name in (".", ".."):
        return toast(t("common.required_field"), "err", status=400)
    try:
        await _files(panel, row, "mkdir", {"path": mc.clean_path(posixpath.join(str(form.get("dir_path") or "/"), name))})
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    except Exception as exc:  # noqa: BLE001
        return toast(str(exc)[:200], "err", status=400)
    return toast(t("containers.folder_created"), extra={"mc-files": True})


# --- PlaceholderAPI i makra -------------------------------------------------------------------------------


@router.post("/minecraft/{server_id}/papi")
async def papi(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
               user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    form = await request.form()
    player = str(form.get("player") or "").strip()
    if player and not mc.PLAYER.match(player):
        return toast(t("mc.bad_player"), "err", status=400)
    try:
        data = await mc.call(panel, row, "papi", {"text": str(form.get("text") or "")[:2000], "player": player})
    except (mc.Offline, mc.BridgeError) as exc:
        return _bridge_error(exc)
    return render(request, "minecraft/_papi_result.html", data=data)


@router.post("/minecraft/{server_id}/macros")
async def save_macros(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    form = await request.form()
    items = mc.macros(row)
    action = form.get("action")
    if action == "add":
        try:
            cmd = mc.clean_command(str(form.get("cmd") or ""))
        except ValueError:
            return toast(t("mc.bad_command"), "err", status=400)
        label = str(form.get("label") or "").strip()[:30] or cmd[:30]
        items = [*items, {"label": label, "cmd": cmd, "icon": "lightning"}][:30]
    elif action == "delete":
        index = int(form.get("index") or -1)
        items = [m for i, m in enumerate(items) if i != index]
    elif action == "reset":
        items = mc.DEFAULT_MACROS
    panel.db.update("minecraft_servers", row["id"], macros=json.dumps(items))
    return redirect(request, f"/minecraft/{row['id']}?tab=console")


# --- ustawienia połączenia --------------------------------------------------------------------------------


@router.post("/minecraft/{server_id}/settings")
async def save_settings(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60] or row["name"]
    panel.db.update("minecraft_servers", row["id"], name=name)
    host = str(form.get("ftp_host") or "").strip()
    if not host:
        mc.set_ftp(panel, row["id"], None)
    else:
        old = mc.get_ftp(panel, row) or {}
        port = str(form.get("ftp_port") or "21")
        mc.set_ftp(panel, row["id"], {"host": host[:200], "port": int(port) if port.isdigit() else 21, "user": str(form.get("ftp_user") or "")[:120],
                                      "password": str(form.get("ftp_password") or "") or old.get("password", ""), "tls": form.get("ftp_tls") == "1"})  # fmt: skip
    return redirect(request, f"/minecraft/{row['id']}?tab=settings", t("mc.settings_saved"))


@router.post("/minecraft/{server_id}/ftp/test")
async def ftp_test(row: dict = Depends(_server), panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    ftp = mc.get_ftp(panel, row)
    if not ftp:
        return toast(t("mc.ftp_missing"), "err", status=400)
    try:
        welcome = await mc.ftp_call(ftp, "test")
    except Exception as exc:  # noqa: BLE001
        return toast(t("mc.ftp_failed", error=str(exc)[:160]), "err", status=400)
    return toast(t("mc.ftp_ok", welcome=str(welcome)[:120]))


@router.post("/minecraft/{server_id}/pair")
async def repair(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    mc.new_pair_code(panel, row["id"])
    return redirect(request, f"/minecraft/{row['id']}?tab=settings", t("mc.pair_reset"))


@router.post("/minecraft/{server_id}/delete")
async def delete_server(request: Request, row: dict = Depends(_server), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    panel.db.delete("minecraft_servers", row["id"])
    mc._links.pop(row["id"], None)
    return redirect(request, "/minecraft", t("mc.deleted", name=row["name"]))


def linked_server(panel: Any, app_id: int) -> dict | None:
    return panel.db.fetchone("SELECT * FROM minecraft_servers WHERE app_id = ?", [app_id])
