"""Punkty końcowe protokołu Model Context Protocol (MCP) dla Deployry.

Obsługuje:
- Direct Streamable HTTP: POST /mcp (JSON-RPC 2.0 bezpośrednio w odpowiedzi)
- Server-Sent Events (SSE): GET /mcp/sse + POST /mcp/messages?session_id=...
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger

from ..context import Panel, get_panel
from ..permissions import Permission, has
from ..services.mcp_server import dispatch_mcp_message
from ..services.tokens import verify_token

router = APIRouter()

# Aktywne sesje SSE: session_id -> (asyncio.Queue, user_dict)
_sse_sessions: dict[str, tuple[asyncio.Queue, dict[str, Any]]] = {}


def _resolve_user(request: Request, panel: Panel) -> dict[str, Any] | None:
    """Rozpoznaje użytkownika z sesji ciasteczka lub nagłówka/parametru Authorization/token."""
    if getattr(request.state, "user", None):
        return request.state.user

    raw_token = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw_token = auth.removeprefix("Bearer ").strip()
    elif request.query_params.get("token"):
        raw_token = request.query_params.get("token", "").strip()

    if raw_token:
        return verify_token(panel, raw_token)
    return None


@router.post("/mcp")
async def mcp_direct_post(request: Request, panel: Panel = Depends(get_panel)) -> Response:
    """Direct HTTP POST JSON-RPC 2.0 endpoint dla klientów MCP (Streamable HTTP)."""
    user = _resolve_user(request, panel)
    if not user:
        return JSONResponse({"error": "Unauthorized: valid Deployra API / MCP token required"}, status_code=401)
    if not has(user, Permission.ALLOW_MCP):
        return JSONResponse({"error": "Forbidden: missing ALLOW_MCP permission"}, status_code=403)

    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}, status_code=400)

    # Obsługa pojedynczego komunikatu lub batcha
    if isinstance(body, list):
        results = []
        for msg in body:
            res = await dispatch_mcp_message(panel, user, msg)
            if res is not None:
                results.append(res)
        return JSONResponse(results)

    result = await dispatch_mcp_message(panel, user, body)
    if result is None:
        return Response(status_code=204)
    return JSONResponse(result)


@router.get("/mcp/sse")
async def mcp_sse_connect(request: Request, panel: Panel = Depends(get_panel)) -> Response:
    """SSE endpoint dla klientów MCP (np. Claude Desktop, Cursor, Antigravity)."""
    user = _resolve_user(request, panel)
    if not user:
        return JSONResponse({"error": "Unauthorized: valid Deployra API / MCP token required"}, status_code=401)
    if not has(user, Permission.ALLOW_MCP):
        return JSONResponse({"error": "Forbidden: missing ALLOW_MCP permission"}, status_code=403)

    session_id = secrets.token_hex(16)
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = (queue, user)

    async def event_generator():
        try:
            # Pierwszy komunikat: podajemy klientowi endpoint do wysyłania wiadomości
            yield f"event: endpoint\r\ndata: /mcp/messages?session_id={session_id}\r\n\r\n"
            while True:
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    pass
                try:
                    # Czekamy na odpowiedź z kolejki lub wysyłamy ping po 15s
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: message\r\ndata: {json.dumps(msg, ensure_ascii=False)}\r\n\r\n"
                except asyncio.TimeoutError:
                    # Heartbeat
                    yield ": ping\r\n\r\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/mcp/messages")
async def mcp_receive_message(request: Request, session_id: str = "", panel: Panel = Depends(get_panel)) -> Response:
    """Klient przesyła JSON-RPC do aktywnej sesji SSE."""
    session = _sse_sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid or expired session_id"}, status_code=404)

    queue, user = session
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}, status_code=400)

    # Dispatch i wrzucenie odpowiedzi do kolejki SSE
    if isinstance(body, list):
        for msg in body:
            res = await dispatch_mcp_message(panel, user, msg)
            if res is not None:
                await queue.put(res)
    else:
        res = await dispatch_mcp_message(panel, user, body)
        if res is not None:
            await queue.put(res)

    return Response(status_code=202)
