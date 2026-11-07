"""WebSSH do węzła: ustawienia, akceptacja klucza hosta, potwierdzenie tożsamości i terminal (xterm ↔ asyncssh)."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from loguru import logger

from deployra_common.passwords import verify_password

from ..context import Panel, get_panel
from ..deps import node_or_404, require
from ..i18n import t
from ..permissions import Permission, has
from ..security import SESSION_COOKIE, Sessions
from ..services import node_ssh
from ..web import redirect, render, toast

router = APIRouter()
SSH = Permission.ALLOW_NODE_SSH


@router.get("/nodes/{node_id}/ssh")
def ssh_page(request: Request, node: dict = Depends(node_or_404), user: dict = Depends(require(SSH))):
    if not node_ssh.configured(node):
        return redirect(request, f"/nodes/{node['id']}/settings#ssh", t("ssh.not_configured"), "warn")
    if not Sessions.reauth_fresh(user, node_ssh.REAUTH_SECONDS):
        return render(request, "nodes/ssh_reauth.html", title=f"WebSSH · {node['name']}", node=node)
    return render(request, "nodes/terminal.html", title=f"WebSSH · {node['name']}", heading=f"WebSSH · {node['name']}", node=node,
                  ssh_mode=True, ep="local", cid="", ws_path=f"/nodes/{node['id']}/ssh/ws")  # fmt: skip


@router.post("/nodes/{node_id}/ssh/reauth")
async def reauth(request: Request, password: str = Form(""), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(SSH))):  # fmt: skip
    """Ponowne potwierdzenie hasłem (i kodem 2FA, gdy konto go ma) przed powłoką na serwerze."""
    from ..services import account_security

    ip = request.client.host if request.client else "?"
    if await run_in_threadpool(panel.limiter.blocked, ip):
        return toast(t("msg.too_many_attempts"), "err", status=429)
    ok = await run_in_threadpool(verify_password, password, user.get("password_hash"))
    if ok and user.get("totp_secret"):
        form = await request.form()
        ok = await run_in_threadpool(account_security.check_code, panel, user, str(form.get("code") or ""))
    if not ok:
        await run_in_threadpool(panel.limiter.fail, ip, user["email"])
        return toast(t("ssh.reauth_failed"), "err", status=403)
    panel.sessions.mark_reauth(user["sid"])
    return redirect(request, f"/nodes/{node['id']}/ssh")


@router.post("/nodes/{node_id}/ssh/settings")
async def save_settings(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel), user: dict = Depends(require(SSH))):
    try:
        node_ssh.save(panel, node, await request.form())
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    return redirect(request, f"/nodes/{node['id']}/settings#ssh", t("ssh.saved"))


@router.post("/nodes/{node_id}/ssh/hostkey")
async def host_key(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel), user: dict = Depends(require(SSH))):
    """Klucz hosta do akceptacji (pobrany bez logowania)."""
    try:
        fingerprint, _ = await node_ssh.fetch_host_key(panel, node)
    except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
        return render(request, "nodes/_ssh_hostkey.html", node=node, error=str(exc) or type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — błąd protokołu SSH
        return render(request, "nodes/_ssh_hostkey.html", node=node, error=str(exc) or type(exc).__name__)
    trusted = node_ssh.settings(node)["fingerprint"]
    return render(request, "nodes/_ssh_hostkey.html", node=node, fingerprint=fingerprint, trusted=trusted)


@router.post("/nodes/{node_id}/ssh/hostkey/accept")
async def accept_host_key(request: Request, fingerprint: str = Form(...), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                          user: dict = Depends(require(SSH))):  # fmt: skip
    """Akceptacja tego odcisku, który użytkownik widział: klucz pobieramy jeszcze raz i porównujemy (nie z formularza)."""
    try:
        current, key = await node_ssh.fetch_host_key(panel, node)
    except Exception as exc:  # noqa: BLE001
        return toast(str(exc) or type(exc).__name__, "err", status=502)
    if current != fingerprint.strip():
        return toast(t("ssh.key_mismatch"), "err", status=409)
    node_ssh.accept_key(panel, node, current, key)
    logger.warning(f"WebSSH: {user['email']} zaufał kluczowi hosta {node['name']} ({current})")
    return redirect(request, f"/nodes/{node['id']}/settings#ssh", t("ssh.key_accepted"))


@router.websocket("/nodes/{node_id}/ssh/ws")
async def ssh_ws(websocket: WebSocket, node_id: int, cols: int = 120, rows: int = 32) -> None:
    panel: Panel = websocket.app.state.panel
    session = await run_in_threadpool(panel.sessions.load, websocket.cookies.get(SESSION_COOKIE))
    if not session or not has(session, SSH) or session.get("scoped"):
        await websocket.close(code=4401)
        return
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=4403)
        return
    if not Sessions.reauth_fresh(session, node_ssh.REAUTH_SECONDS):
        await websocket.close(code=4428)  # strona poprosi o hasło / 2FA
        return
    node = panel.node(node_id)
    if not node or not node_ssh.configured(node):
        await websocket.close(code=4400)
        return
    await websocket.accept()
    started = time.time()
    try:
        conn = await node_ssh.connect(panel, node)
    except node_ssh.HostKeyError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close()
        return
    except Exception as exc:  # noqa: BLE001 — błąd połączenia/logowania trafia do terminala
        await websocket.send_text(json.dumps({"type": "error", "message": t("ssh.connect_failed", error=str(exc) or type(exc).__name__)}))
        await websocket.close()
        return
    logger.info(f"WebSSH: {session['email']} → {node['name']}")
    from ..services import audit

    ip = websocket.client.host if websocket.client else ""
    await run_in_threadpool(audit.record, panel, "ssh_open", user=session, ip=ip, target=node["name"])
    last = {"at": time.time()}
    try:
        async with conn:
            process = await conn.create_process(term_type="xterm-256color", term_size=(max(cols, 10), max(rows, 2)), encoding=None)

            async def to_browser() -> None:
                while True:
                    data = await process.stdout.read(65536)
                    if not data:
                        return
                    await websocket.send_bytes(data)

            async def to_server() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes"):
                        last["at"] = time.time()
                        process.stdin.write(message["bytes"])
                    elif message.get("text"):
                        try:
                            control = json.loads(message["text"])
                        except ValueError:
                            continue
                        if control.get("type") == "resize":
                            process.change_terminal_size(max(int(control.get("cols", 80)), 10), max(int(control.get("rows", 24)), 2))

            async def idle() -> None:
                while time.time() - last["at"] < node_ssh.IDLE_SECONDS:
                    await asyncio.sleep(30)

            tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_server()), asyncio.create_task(idle())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if tasks[2] in done:
                await websocket.send_text(json.dumps({"type": "error", "message": t("ssh.idle")}))
            process.close()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"WebSSH {node['name']}: {exc}")
    finally:
        logger.info(f"WebSSH: {session['email']} ← {node['name']} ({int(time.time() - started)} s)")
        await run_in_threadpool(audit.record, panel, "ssh_close", user=session, ip=ip, target=node["name"], seconds=int(time.time() - started))
        try:
            await websocket.send_text(json.dumps({"type": "exit"}))
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
