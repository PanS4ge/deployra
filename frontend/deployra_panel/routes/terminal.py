"""Terminal w kontenerze: przeglądarka (xterm.js) ↔ panel ↔ węzeł ↔ docker exec."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode, urlparse

import websockets
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from loguru import logger

from ..context import Panel
from ..deps import node_or_404, require
from ..i18n import t
from ..permissions import Permission, has
from ..security import SESSION_COOKIE
from ..web import render

router = APIRouter()
_EP = re.compile(r"^(local|ct-\d{1,9})$")
_CID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


@router.get("/nodes/{node_id}/containers/{cid}/terminal")
def terminal_page(request: Request, cid: str, node: dict = Depends(node_or_404),
                  user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    return render(request, "nodes/terminal.html", title=f"Terminal · {cid}", node=node, cid=cid)


@router.websocket("/nodes/{node_id}/containers/{cid}/terminal/ws")
async def terminal_ws(websocket: WebSocket, node_id: int, cid: str, ep: str = "local", shell: str = "auto",
                      cols: int = 120, rows: int = 32) -> None:  # fmt: skip
    panel: Panel = websocket.app.state.panel
    session = await run_in_threadpool(panel.sessions.load, websocket.cookies.get(SESSION_COOKIE))
    if not session or not has(session, Permission.ALLOW_CONTAINER_TERMINAL) or session.get("scoped"):
        await websocket.close(code=4401)
        return
    await relay_terminal(websocket, panel, session, panel.node(node_id), cid, ep, shell, cols, rows)


async def relay_terminal(websocket: WebSocket, panel: Panel, session: dict, node: dict | None, cid: str, ep: str, shell: str,
                         cols: int, rows: int) -> None:  # fmt: skip
    """Przeglądarka ↔ węzeł (docker exec). Uprawnienia sprawdza wywołujący; tu origin i poprawność parametrów."""
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=4403)  # obrona przed cross-site WebSocket hijacking
        return
    if not node or not _EP.match(ep) or not _CID.match(cid) or shell not in ("auto", "sh", "bash", "ash"):
        await websocket.close(code=4400)
        return
    client = panel.node_client(node)
    path = f"/api/containers/{cid}/exec?" + urlencode({"shell": shell, "cols": max(cols, 10), "rows": max(rows, 2), "ep": ep})
    await websocket.accept()
    logger.info(f"Terminal: {session['email']} → {node['name']}/{ep}/{cid}")
    try:
        async with websockets.connect(client.ws_url(path), additional_headers=client.ws_headers(path), max_size=None,
                                      open_timeout=15) as remote:  # fmt: skip

            async def to_remote() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await remote.send(message["bytes"])
                    elif message.get("text") is not None:
                        await remote.send(message["text"])

            async def to_browser() -> None:
                async for data in remote:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)

            tasks = [asyncio.create_task(to_remote()), asyncio.create_task(to_browser())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
    except (OSError, websockets.WebSocketException) as exc:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": t("msg.terminal_connect_failed", error=exc)}))
        except Exception:  # noqa: BLE001
            pass
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — już zamknięty
            pass
