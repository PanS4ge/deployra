"""Klient HTTP z podpisem HMAC (panel → bramka, panel → węzeł)."""

from __future__ import annotations

import hashlib
import json as jsonlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from . import i18n
from .hmac_auth import sign


class RemoteError(Exception):
    """Zdalny komponent zwrócił błąd."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and "detail" in data:
            detail = data["detail"]
            return detail if isinstance(detail, str) else jsonlib.dumps(detail, ensure_ascii=False)
    except ValueError:
        pass
    return response.text[:500] or response.reason_phrase


class SignedClient:
    def __init__(
        self,
        base_url: str,
        secret: bytes,
        *,
        key_id: str = "panel",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.key_id = key_id
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _signed(self, request: httpx.Request, body_sha256: str | None = None) -> httpx.Request:
        path = request.url.raw_path.decode()
        body = b"" if body_sha256 else request.read()
        request.headers["Accept-Language"] = i18n.current.get()  # komunikaty i logi w języku użytkownika panelu
        request.headers.update(
            sign(self.secret, request.method, path, body, key_id=self.key_id, body_sha256=body_sha256)
        )
        return request

    def build(self, method: str, path: str, **kwargs: Any) -> httpx.Request:
        return self._signed(self._client.build_request(method, path, **kwargs))

    async def request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> httpx.Response:
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = await self._client.send(self.build(method, path, **kwargs))
        if response.status_code >= 400:
            raise RemoteError(response.status_code, _detail(response))
        return response

    async def get(self, path: str, **kwargs: Any) -> Any:
        return (await self.request("GET", path, **kwargs)).json()

    async def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        response = await self.request("POST", path, json=json, **kwargs)
        return response.json() if response.content else None

    async def put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        response = await self.request("PUT", path, json=json, **kwargs)
        return response.json() if response.content else None

    async def delete(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("DELETE", path, **kwargs)
        return response.json() if response.content else None

    @asynccontextmanager
    async def stream(self, method: str, path: str, **kwargs: Any) -> AsyncIterator[httpx.Response]:
        """Strumień odpowiedzi (np. SSE z logami). Bez limitu czasu odczytu."""
        kwargs.setdefault("timeout", httpx.Timeout(10.0, read=None))
        request = self.build(method, path, **kwargs)
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code >= 400:
                await response.aread()
                raise RemoteError(response.status_code, _detail(response))
            yield response
        finally:
            await response.aclose()

    async def upload_file(self, path: str, file_path: str, *, timeout: float = 600.0) -> Any:
        """Wysyła duży plik strumieniowo; podpis obejmuje skrót SHA-256 pliku."""
        digest = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)

        async def body() -> AsyncIterator[bytes]:
            with open(file_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    yield chunk

        request = self._client.build_request(
            "POST", path, content=body(), headers={"Content-Type": "application/octet-stream"}, timeout=timeout
        )
        response = await self._client.send(self._signed(request, body_sha256=digest.hexdigest()))
        if response.status_code >= 400:
            raise RemoteError(response.status_code, _detail(response))
        return response.json() if response.content else None

    def ws_headers(self, path: str) -> dict[str, str]:
        """Nagłówki podpisu do otwarcia WebSocketu (terminal)."""
        return sign(self.secret, "GET", path, b"", key_id=self.key_id)

    def ws_url(self, path: str) -> str:
        return self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + path
