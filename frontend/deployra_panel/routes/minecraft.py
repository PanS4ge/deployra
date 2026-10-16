"""Obsługa serwerów Minecraft (mostek DeployraBridge dla hostingów współdzielonych + klient FTP)."""

from __future__ import annotations

import asyncio
import io
import posixpath
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from ..context import Panel, get_panel
from ..deps import current_user
from ..services.ftp_client import FtpClientService
from ..web import redirect, render, toast

public = APIRouter()
router = APIRouter(dependencies=[Depends(current_user)])

JAR_PATH = Path(__file__).parent.parent / "static" / "downloads" / "DeployraBridge-1.0.0.jar"

DEFAULT_MACROS: list[dict[str, str]] = [
    {"name": "☀️ Dzień", "cmd": "time set day", "icon": "bi-sun", "color": "#f1c40f"},
    {"name": "🌙 Noc", "cmd": "time set night", "icon": "bi-moon-stars", "color": "#9b59b6"},
    {"name": "⚡ Pogoda: Czyste niebo", "cmd": "weather clear", "icon": "bi-cloud-sun", "color": "#3498db"},
    {"name": "💾 Zapisz świat", "cmd": "save-all", "icon": "bi-save", "color": "#2ecc71"},
    {"name": "📦 Przeładuj serwer", "cmd": "reload confirm", "icon": "bi-arrow-repeat", "color": "#e67e22"},
    {"name": "🧹 Wyczyść przedmioty", "cmd": "kill @e[type=item]", "icon": "bi-trash", "color": "#e74c3c"},
    {"name": "❤️ Ulecz wszystkich", "cmd": "effect give @a instant_health 1 255", "icon": "bi-heart-pulse", "color": "#e74c3c"},
    {"name": "🍗 Nakarm wszystkich", "cmd": "effect give @a saturation 1 255", "icon": "bi-egg-fried", "color": "#f39c12"},
    {"name": "🛡️ Whitelist ON", "cmd": "whitelist on", "icon": "bi-shield-check", "color": "#27ae60"},
    {"name": "🔓 Whitelist OFF", "cmd": "whitelist off", "icon": "bi-shield-slash", "color": "#c0392b"},
    {"name": "👥 Lista graczy", "cmd": "list", "icon": "bi-people", "color": "#34495e"},
    {"name": "⚡ Sprawdź TPS", "cmd": "tps", "icon": "bi-speedometer2", "color": "#1abc9c"},
]


def _get_servers(panel: Panel) -> list[dict[str, Any]]:
    return panel.db.setting("minecraft_servers", [])


def _save_servers(panel: Panel, servers: list[dict[str, Any]]) -> None:
    panel.db.set_setting("minecraft_servers", servers)


def _get_server(panel: Panel, server_id: int) -> dict[str, Any] | None:
    servers = _get_servers(panel)
    return next((s for s in servers if s.get("id") == server_id), None)


def _update_server(panel: Panel, server_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
    servers = _get_servers(panel)
    for s in servers:
        if s.get("id") == server_id:
            s.update(updates)
            _save_servers(panel, servers)
            return s
    return None


def _get_ftp(server: dict[str, Any]) -> FtpClientService | None:
    host = server.get("ftp_host") or server.get("host")
    if not host or not server.get("ftp_user"):
        return None
    return FtpClientService(
        host=host,
        port=server.get("ftp_port", 21),
        username=server.get("ftp_user", ""),
        password=server.get("ftp_pass", ""),
        use_tls=server.get("ftp_tls", False),
        timeout=8.0,
    )


async def _fetch_status(server: dict[str, Any]) -> dict[str, Any]:
    url = f"http://{server['host']}:{server['port']}/api/info"
    headers = {"Authorization": f"Bearer {server.get('token', '')}"}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                server["online"] = True
                server["data"] = data
                return server
    except Exception:
        pass
    server["online"] = False
    server["data"] = {}
    return server


async def _fetch_players(server: dict[str, Any]) -> list[dict[str, Any]]:
    if not server.get("online"):
        return []
    url = f"http://{server['host']}:{server['port']}/api/players"
    headers = {"Authorization": f"Bearer {server.get('token', '')}"}
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json().get("players", [])
    except Exception:
        pass
    return []


async def _fetch_properties(server: dict[str, Any]) -> dict[str, str]:
    # Próba 1: Jeśli FTP jest skonfigurowane, czytamy server.properties z FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            raw = ftp.read_text("server.properties")
            props: dict[str, str] = {}
            for line in raw.splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "!")):
                    eq = line.find("=")
                    if eq > 0:
                        props[line[:eq].strip()] = line[eq + 1:].strip()
            return props
        except Exception:
            pass

    # Próba 2: Przez bridge plugin
    if server.get("online"):
        url = f"http://{server['host']}:{server['port']}/api/properties"
        headers = {"Authorization": f"Bearer {server.get('token', '')}"}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.json().get("properties", {})
        except Exception:
            pass

    return {}


async def _save_properties(server: dict[str, Any], updates: dict[str, str]) -> tuple[bool, str]:
    # Próba 1: FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            raw = ""
            try:
                raw = ftp.read_text("server.properties")
            except Exception:
                pass

            existing_lines = raw.splitlines()
            new_lines = []
            seen_keys = set()
            for line in existing_lines:
                trimmed = line.strip()
                if trimmed and not trimmed.startswith(("#", "!")):
                    eq = trimmed.find("=")
                    if eq > 0:
                        k = trimmed[:eq].strip()
                        if k in updates:
                            new_lines.append(f"{k}={updates[k]}")
                            seen_keys.add(k)
                            continue
                new_lines.append(line)

            for k, v in updates.items():
                if k not in seen_keys:
                    new_lines.append(f"{k}={v}")

            ftp.write_text("server.properties", "\n".join(new_lines) + "\n")
            return True, "Zapisano server.properties przez FTP."
        except Exception as e:
            return False, f"Błąd zapisu przez FTP: {e}"

    # Próba 2: Bridge Plugin
    if server.get("online"):
        url = f"http://{server['host']}:{server['port']}/api/properties"
        headers = {"Authorization": f"Bearer {server.get('token', '')}"}
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.post(url, headers=headers, json={"properties": updates})
                if resp.status_code == 200:
                    return True, "Zapisano server.properties przez DeployraBridge."
                return False, f"Błąd bridge {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Błąd połączenia z bridge: {e}"

    return False, "Brak połączenia z FTP ani z pluginem DeployraBridge."


@public.get("/minecraft/download")
def download_plugin():
    if not JAR_PATH.exists():
        raise HTTPException(404, "Plik pluginu DeployraBridge nie został odnaleziony.")
    return FileResponse(
        JAR_PATH,
        filename="DeployraBridge-1.0.0.jar",
        media_type="application/java-archive",
    )


@router.get("/minecraft")
async def list_page(request: Request, panel: Panel = Depends(get_panel)):
    servers = _get_servers(panel)
    if servers:
        tasks = [_fetch_status(dict(s)) for s in servers]
        servers_with_status = await asyncio.gather(*tasks)
    else:
        servers_with_status = []

    return render(
        request,
        "minecraft/list.html",
        title="Serwery Minecraft",
        section="minecraft",
        servers=servers_with_status,
    )


@router.post("/minecraft/add")
async def add_server(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(25585),
    token: str = Form(""),
    ftp_host: str = Form(""),
    ftp_port: int = Form(21),
    ftp_user: str = Form(""),
    ftp_pass: str = Form(""),
    ftp_tls: bool = Form(False),
    ftp_enabled: bool = Form(False),
    panel: Panel = Depends(get_panel),
):
    host = host.strip()
    name = name.strip() or f"Minecraft ({host}:{port})"
    token = token.strip()

    test_url = f"http://{host}:{port}/health"
    online = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(test_url)
            if resp.status_code == 200:
                online = True
    except Exception:
        pass

    servers = _get_servers(panel)
    new_id = max([s.get("id", 0) for s in servers], default=0) + 1

    servers.append({
        "id": new_id,
        "name": name,
        "host": host,
        "port": port,
        "token": token,
        "ftp_host": ftp_host.strip() or host,
        "ftp_port": ftp_port,
        "ftp_user": ftp_user.strip(),
        "ftp_pass": ftp_pass,
        "ftp_tls": bool(ftp_tls),
        "ftp_enabled": bool(ftp_enabled or ftp_user.strip()),
        "macros": list(DEFAULT_MACROS),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_servers(panel, servers)

    msg = f"Serwer '{name}' został dodany!" if online else f"Serwer '{name}' został dodany (obecnie offline)."
    return redirect(request, f"/minecraft/{new_id}", msg)


@router.get("/minecraft/{server_id}")
async def detail_page(
    request: Request,
    server_id: int,
    tab: str = Query("console"),
    player: str = Query(""),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer Minecraft nie został odnaleziony.")

    # Inicjalizacja makr jeśli serwer ich jeszcze nie ma
    if "macros" not in server or not server["macros"]:
        server["macros"] = list(DEFAULT_MACROS)
        _update_server(panel, server_id, {"macros": server["macros"]})

    server_status = await _fetch_status(dict(server))
    data = server_status.get("data", {})

    # Pobierz graczy
    players_rich = []
    if server_status["online"]:
        players_rich = await _fetch_players(server_status)

    # Pobierz właściwości serwera
    properties = await _fetch_properties(server_status)

    # Pobierz logi
    logs = []
    if server_status["online"]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(
                    f"http://{server['host']}:{server['port']}/api/logs",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                )
                if r.status_code == 200:
                    logs = r.json().get("lines", [])
        except Exception:
            pass

    # Sprawdź stan PAPI
    papi_status: dict[str, Any] = {"enabled": False, "version": None, "expansions": []}
    if server_status["online"]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(
                    f"http://{server['host']}:{server['port']}/api/papi/status",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                )
                if r.status_code == 200:
                    papi_status = r.json()
        except Exception:
            pass

    # Stan połączenia FTP
    ftp_ok = False
    ftp_msg = "Nie skonfigurowano FTP"
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        ftp_ok, ftp_msg = ftp.test_connection()

    return render(
        request,
        "minecraft/detail.html",
        title=f"Minecraft: {server['name']}",
        section="minecraft",
        server=server_status,
        data=data,
        players_rich=players_rich,
        properties=properties,
        logs=logs,
        papi=papi_status,
        ftp_ok=ftp_ok,
        ftp_msg=ftp_msg,
        default_macros=DEFAULT_MACROS,
        active_tab=tab,
        active_player=player,
    )


@router.post("/minecraft/{server_id}/settings")
async def update_settings(
    request: Request,
    server_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    token: str = Form(""),
    ftp_host: str = Form(""),
    ftp_port: int = Form(21),
    ftp_user: str = Form(""),
    ftp_pass: str = Form(""),
    ftp_tls: bool = Form(False),
    ftp_enabled: bool = Form(False),
    panel: Panel = Depends(get_panel),
):
    updates = {
        "name": name.strip(),
        "host": host.strip(),
        "port": port,
        "token": token.strip(),
        "ftp_host": ftp_host.strip(),
        "ftp_port": ftp_port,
        "ftp_user": ftp_user.strip(),
        "ftp_pass": ftp_pass,
        "ftp_tls": bool(ftp_tls),
        "ftp_enabled": bool(ftp_enabled or ftp_user.strip()),
    }
    _update_server(panel, server_id, updates)
    return redirect(request, f"/minecraft/{server_id}?tab=settings", "Ustawienia serwera zostały zaktualizowane.")


@router.post("/minecraft/{server_id}/ftp/test")
async def test_ftp(request: Request, server_id: int, panel: Panel = Depends(get_panel)):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "message": "Serwer nie istnieje."}, status_code=404)
    ftp = _get_ftp(server)
    if not ftp:
        return JSONResponse({"ok": False, "message": "Uzupełnij dane logowania FTP (host, użytkownik, hasło)."})
    ok, msg = ftp.test_connection()
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/minecraft/{server_id}/command")
async def execute_command(
    request: Request,
    server_id: int,
    command: str = Form(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer nie został odnaleziony.")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"http://{server['host']}:{server['port']}/api/command",
                headers={"Authorization": f"Bearer {server.get('token', '')}"},
                json={"command": command},
            )
            if resp.status_code == 200:
                res = resp.json()
                return JSONResponse({"ok": True, "output": res.get("output", "Wykonano.")})
            return JSONResponse({"ok": False, "output": f"Błąd {resp.status_code}: {resp.text}"}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "output": f"Błąd połączenia z serwerem: {e}"}, status_code=500)


@router.post("/minecraft/{server_id}/action")
async def execute_action(
    request: Request,
    server_id: int,
    action: str = Form(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer nie został odnaleziony.")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"http://{server['host']}:{server['port']}/api/action",
                headers={"Authorization": f"Bearer {server.get('token', '')}"},
                json={"action": action},
            )
            if resp.status_code == 200:
                return redirect(request, f"/minecraft/{server_id}", f"Akcja '{action}' wykonana pomyślnie.")
    except Exception:
        pass
    return redirect(request, f"/minecraft/{server_id}", f"Nie udało się wykonać akcji '{action}'.")


@router.post("/minecraft/{server_id}/players/action")
async def player_action(
    request: Request,
    server_id: int,
    player: str = Form(...),
    action: str = Form(...),
    value: str = Form(""),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(
                f"http://{server['host']}:{server['port']}/api/players/action",
                headers={"Authorization": f"Bearer {server.get('token', '')}"},
                json={"player": player, "action": action, "value": value},
            )
            if resp.status_code == 200:
                return JSONResponse(resp.json())
            return JSONResponse({"ok": False, "error": f"Błąd {resp.status_code}: {resp.text}"}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Błąd połączenia: {e}"}, status_code=500)


@router.get("/minecraft/{server_id}/properties")
async def get_properties_endpoint(server_id: int, panel: Panel = Depends(get_panel)):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)
    props = await _fetch_properties(server)
    return JSONResponse({"ok": True, "properties": props})


@router.post("/minecraft/{server_id}/properties")
async def save_properties_endpoint(
    request: Request,
    server_id: int,
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer nie znaleziony.")

    form_data = await request.form()
    updates: dict[str, str] = {}
    for k, v in form_data.items():
        if k not in ("_csrf_token", "server_id"):
            updates[k] = str(v)

    ok, msg = await _save_properties(server, updates)
    return redirect(request, f"/minecraft/{server_id}?tab=options", msg)


# --- FILE MANAGER ENDPOINTS (FTP + BRIDGE FALLBACK) ---


@router.get("/minecraft/{server_id}/files/list")
async def files_list(server_id: int, path: str = Query("/"), panel: Panel = Depends(get_panel)):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    # 1. Próba FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            items = ftp.list_dir(path)
            return JSONResponse({
                "ok": True,
                "path": path,
                "mode": "ftp",
                "files": [i.to_dict() for i in items],
            })
        except Exception as e:
            # Jeśli FTP zawiedzie, próbujemy bridge plugin
            pass

    # 2. Próba Bridge Plugin
    if server.get("online"):
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(
                    f"http://{server['host']}:{server['port']}/api/files/list",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    params={"path": path},
                )
                if r.status_code == 200:
                    data = r.json()
                    data["mode"] = "bridge"
                    return JSONResponse(data)
        except Exception:
            pass

    return JSONResponse({
        "ok": False,
        "error": "Nie udało się pobrać listy plików (brak połączenia FTP i Bridge).",
    }, status_code=500)


@router.get("/minecraft/{server_id}/files/read")
async def files_read(server_id: int, path: str = Query(...), panel: Panel = Depends(get_panel)):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    # 1. FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            text = ftp.read_text(path)
            return JSONResponse({"ok": True, "path": path, "content": text})
        except Exception:
            pass

    # 2. Bridge Plugin
    if server.get("online"):
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(
                    f"http://{server['host']}:{server['port']}/api/files/read",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    params={"path": path},
                )
                if r.status_code == 200:
                    return JSONResponse(r.json())
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": False, "error": "Brak możliwości odczytu pliku."}, status_code=500)


@router.post("/minecraft/{server_id}/files/write")
async def files_write(
    server_id: int,
    path: str = Form(...),
    content: str = Form(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    # 1. FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            ftp.write_text(path, content)
            return JSONResponse({"ok": True, "path": path, "message": "Zapisano przez FTP."})
        except Exception:
            pass

    # 2. Bridge Plugin
    if server.get("online"):
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.post(
                    f"http://{server['host']}:{server['port']}/api/files/write",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    json={"path": path, "content": content},
                )
                if r.status_code == 200:
                    return JSONResponse(r.json())
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": False, "error": "Nie udało się zapisać pliku."}, status_code=500)


@router.post("/minecraft/{server_id}/files/upload")
async def files_upload(
    server_id: int,
    path: str = Form("/"),
    file: UploadFile = File(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    data = await file.read()
    dest_path = posixpath.join(path, file.filename or "upload.bin")

    # 1. FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            ftp.upload_bytes(dest_path, data)
            return JSONResponse({"ok": True, "path": dest_path, "message": f"Wgrano {file.filename} przez FTP."})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Błąd uploadu FTP: {e}"}, status_code=500)

    # 2. Bridge fallback (dla plików tekstowych lub małych)
    if server.get("online"):
        try:
            text_content = data.decode("utf-8", errors="replace")
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    f"http://{server['host']}:{server['port']}/api/files/write",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    json={"path": dest_path, "content": text_content},
                )
                if r.status_code == 200:
                    return JSONResponse({"ok": True, "path": dest_path, "message": "Wgrano przez bridge."})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": False, "error": "Brak skonfigurowanego FTP do wgrywania plików."}, status_code=500)


@router.post("/minecraft/{server_id}/files/delete")
async def files_delete(
    server_id: int,
    path: str = Form(...),
    is_dir: bool = Form(False),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    # 1. FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            ftp.delete(path, is_dir=is_dir)
            return JSONResponse({"ok": True, "message": "Usunięto pomyślnie."})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # 2. Bridge Plugin
    if server.get("online"):
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.post(
                    f"http://{server['host']}:{server['port']}/api/files/delete",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    json={"path": path},
                )
                if r.status_code == 200:
                    return JSONResponse(r.json())
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": False, "error": "Nie udało się usunąć obiektu."}, status_code=500)


@router.post("/minecraft/{server_id}/files/mkdir")
async def files_mkdir(
    server_id: int,
    path: str = Form(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    # 1. FTP
    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            ftp.mkdir(path)
            return JSONResponse({"ok": True, "message": "Utworzono folder przez FTP."})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # 2. Bridge Plugin
    if server.get("online"):
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.post(
                    f"http://{server['host']}:{server['port']}/api/files/mkdir",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    json={"path": path},
                )
                if r.status_code == 200:
                    return JSONResponse(r.json())
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": False, "error": "Nie udało się utworzyć folderu."}, status_code=500)


@router.get("/minecraft/{server_id}/files/download")
async def files_download(
    server_id: int,
    path: str = Query(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer nie znaleziony.")

    ftp = _get_ftp(server)
    if ftp and server.get("ftp_enabled"):
        try:
            data = ftp.download_bytes(path)
            filename = posixpath.basename(path) or "download"
            return Response(
                content=data,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as e:
            raise HTTPException(500, f"Błąd pobierania pliku przez FTP: {e}")

    # Bridge read text fallback
    if server.get("online"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"http://{server['host']}:{server['port']}/api/files/read",
                    headers={"Authorization": f"Bearer {server.get('token', '')}"},
                    params={"path": path},
                )
                if r.status_code == 200:
                    data_str = r.json().get("content", "")
                    filename = posixpath.basename(path) or "download.txt"
                    return Response(
                        content=data_str.encode("utf-8"),
                        media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                    )
        except Exception as e:
            raise HTTPException(500, f"Błąd pobierania pliku przez Bridge: {e}")

    raise HTTPException(500, "Brak aktywnego połączenia FTP ani Bridge.")


# --- PLACEHOLDERAPI (PAPI) ENDPOINTS ---


@router.get("/minecraft/{server_id}/papi/status")
async def papi_status_endpoint(server_id: int, panel: Panel = Depends(get_panel)):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    if not server.get("online"):
        return JSONResponse({"ok": False, "error": "Serwer jest offline."})

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(
                f"http://{server['host']}:{server['port']}/api/papi/status",
                headers={"Authorization": f"Bearer {server.get('token', '')}"},
            )
            if r.status_code == 200:
                return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

    return JSONResponse({"ok": False, "enabled": False})


@router.post("/minecraft/{server_id}/papi/parse")
async def papi_parse_endpoint(
    request: Request,
    server_id: int,
    player: str = Form(""),
    placeholders: str = Form(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Serwer nie znaleziony."}, status_code=404)

    ph_list = [p.strip() for p in placeholders.replace("\r", "\n").split("\n") if p.strip()]

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.post(
                f"http://{server['host']}:{server['port']}/api/papi/parse",
                headers={"Authorization": f"Bearer {server.get('token', '')}"},
                json={"player": player.strip() or None, "placeholders": ph_list},
            )
            if r.status_code == 200:
                return JSONResponse(r.json())
            return JSONResponse({"ok": False, "error": f"Błąd {r.status_code}: {r.text}"}, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# --- MACROS ENDPOINTS ---


@router.post("/minecraft/{server_id}/macros/add")
async def add_macro(
    request: Request,
    server_id: int,
    name: str = Form(...),
    cmd: str = Form(...),
    icon: str = Form("bi-lightning"),
    color: str = Form("#3498db"),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer nie znaleziony.")

    macros = list(server.get("macros", DEFAULT_MACROS))
    macros.append({
        "name": name.strip(),
        "cmd": cmd.strip(),
        "icon": icon.strip() or "bi-lightning",
        "color": color.strip() or "#3498db",
    })
    _update_server(panel, server_id, {"macros": macros})
    return redirect(request, f"/minecraft/{server_id}?tab=console", f"Dodano makro '{name}'.")


@router.post("/minecraft/{server_id}/macros/delete")
async def delete_macro(
    request: Request,
    server_id: int,
    index: int = Form(...),
    panel: Panel = Depends(get_panel),
):
    server = _get_server(panel, server_id)
    if not server:
        raise HTTPException(404, "Serwer nie znaleziony.")

    macros = list(server.get("macros", DEFAULT_MACROS))
    if 0 <= index < len(macros):
        removed = macros.pop(index)
        _update_server(panel, server_id, {"macros": macros})
        return redirect(request, f"/minecraft/{server_id}?tab=console", f"Usunięto makro '{removed['name']}'.")
    return redirect(request, f"/minecraft/{server_id}?tab=console", "Nieprawidłowy indeks makra.")


@router.post("/minecraft/{server_id}/macros/reset")
async def reset_macros(
    request: Request,
    server_id: int,
    panel: Panel = Depends(get_panel),
):
    _update_server(panel, server_id, {"macros": list(DEFAULT_MACROS)})
    return redirect(request, f"/minecraft/{server_id}?tab=console", "Zresetowano makra do wartości domyślnych.")


@router.post("/minecraft/{server_id}/delete")
async def delete_server(request: Request, server_id: int, panel: Panel = Depends(get_panel)):
    servers = _get_servers(panel)
    filtered = [s for s in servers if s["id"] != server_id]
    _save_servers(panel, filtered)
    return redirect(request, "/minecraft", "Serwer został usunięty.")
