"""Weryfikacja podpisów po stronie serwera (FastAPI/Starlette)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from fastapi import HTTPException, Request, WebSocket

from .hmac_auth import H_BODY, NonceCache, SignatureError, verify

SecretLookup = Callable[[str], bytes | None]


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
            raise HTTPException(status_code=401, detail="nieznany klucz lub komponent niesparowany")
        return secret

    async def __call__(self, request: Request) -> str:
        """Zwykłe żądania: podpis obejmuje całe body."""
        if H_BODY.lower() in request.headers:
            raise HTTPException(status_code=400, detail="upload strumieniowy niedozwolony na tym endpoincie")
        return self._verify(request, await request.body())

    async def upload(self, request: Request) -> str:
        """Uploady: podpis obejmuje zadeklarowany skrót; route musi użyć `receive_verified_upload`."""
        if H_BODY.lower() not in request.headers:
            raise HTTPException(status_code=400, detail="brak skrótu pliku")
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
        raise HTTPException(status_code=400, detail="brak skrótu pliku")
    digest = hashlib.sha256()
    size = 0
    with open(destination, "wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="plik jest za duży")
            digest.update(chunk)
            fh.write(chunk)
    if digest.hexdigest() != expected:
        raise HTTPException(status_code=400, detail="skrót pliku się nie zgadza")
    return size
