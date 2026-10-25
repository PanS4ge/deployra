"""Weryfikacja podpisów po stronie serwera (FastAPI/Starlette)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from .hmac_auth import H_BODY, NonceCache, SignatureError, verify
from .i18n import tr

SecretLookup = Callable[[str], bytes | None]
BODY_KEY = "deployra.body"  # body zwykłego żądania wczytane przez BufferBody (w scope ASGI)


class BufferBody:
    """ASGI: body zwykłego podpisanego żądania wczytane raz, zanim zobaczy je FastAPI.

    FastAPI parsuje formularz (multipart z plikami) przed zależnościami trasy, więc strażnik podpisu dostawał już
    zużyty strumień („Stream consumed”). Teraz podpis sprawdzamy na bajtach z `scope`, a trasa czyta je jeszcze raz.
    Uploady strumieniowe (podpis po zadeklarowanym SHA-256) przechodzą bez buforowania."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS") or \
                any(name.lower() == H_BODY.lower().encode() for name, _ in scope.get("headers") or []):  # fmt: skip
            await self.app(scope, receive, send)
            return
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":  # klient się rozłączył
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        scope[BODY_KEY] = body
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _path(scope: dict) -> str:
    raw = scope.get("raw_path") or scope["path"].encode()
    query = scope.get("query_string") or b""
    return (raw + (b"?" + query if query else b"")).decode()


class SignatureGuard:
    """Zależność FastAPI: wpuszcza tylko żądania podpisane znanym kluczem."""

    def __init__(self, lookup: SecretLookup, nonces: NonceCache | None = None) -> None:
        self.lookup = lookup
        self.nonces = nonces or NonceCache()

    def _secret(self, headers) -> bytes:
        key_id = headers.get("x-deployra-key") or ""
        secret = self.lookup(key_id)
        if not secret:
            raise HTTPException(status_code=401, detail=tr("srv.auth.unknown_key"))
        return secret

    async def __call__(self, request: Request) -> str:
        """Zwykłe żądania: podpis obejmuje całe body."""
        if H_BODY.lower() in request.headers:
            raise HTTPException(status_code=400, detail=tr("srv.auth.stream_not_allowed"))
        body = request.scope.get(BODY_KEY)
        return self._verify(request, body if body is not None else await request.body())

    async def upload(self, request: Request) -> str:
        """Uploady: podpis obejmuje zadeklarowany skrót; route musi użyć `receive_verified_upload`."""
        if H_BODY.lower() not in request.headers:
            raise HTTPException(status_code=400, detail=tr("srv.auth.no_digest"))
        return self._verify(request, None)

    def _verify(self, request: Request, body: bytes | None) -> str:
        secret = self._secret(request.headers)
        try:
            return verify(secret, request.method, _path(request.scope), body, request.headers, nonces=self.nonces)
        except SignatureError as exc:
            raise HTTPException(status_code=401, detail=f"podpis: {exc}") from exc

    def check_websocket(self, websocket: WebSocket) -> str:
        secret = self._secret(websocket.headers)
        try:
            return verify(secret, "GET", _path(websocket.scope), b"", websocket.headers, nonces=self.nonces)
        except SignatureError as exc:
            raise PermissionError(str(exc)) from exc


async def receive_verified_upload(request: Request, destination: str, max_bytes: int) -> int:
    """Zapisuje strumieniowany upload na dysk i sprawdza zadeklarowany SHA-256."""
    expected = request.headers.get(H_BODY.lower())
    if not expected:
        raise HTTPException(status_code=400, detail=tr("srv.auth.no_digest"))
    digest = hashlib.sha256()
    size = 0
    with open(destination, "wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail=tr("srv.auth.too_big"))
            digest.update(chunk)
            fh.write(chunk)
    if digest.hexdigest() != expected:
        raise HTTPException(status_code=400, detail=tr("srv.auth.digest_mismatch"))
    return size
