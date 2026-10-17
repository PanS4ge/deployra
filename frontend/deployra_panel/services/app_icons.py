"""Ikony aplikacji: własna → favicon domeny → logo obrazu z rejestru.

Panel pobiera ikonę sam i trzyma kopię na dysku (7 dni, brak — 1 dzień), więc przeglądarka nie zdradza domen
aplikacji zewnętrznym serwisom i nie czeka na nie przy każdym wejściu na listę.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

MAX_BYTES = 256 * 1024
TTL = 7 * 86400
MISS_TTL = 86400
UPLOAD_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/x-icon": "ico",
                "image/vnd.microsoft.icon": "ico"}  # fmt: skip
FETCH_TYPES = {*UPLOAD_TYPES, "image/svg+xml"}  # SVG tylko z sieci, serwowany z CSP bez skryptów
EXT_TYPES = {v: k for k, v in UPLOAD_TYPES.items()}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Deployra-Icons/1.0)", "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"}
_locks: dict[int, asyncio.Lock] = {}


def icon_dir(panel: Any) -> Path:
    path = Path(panel.config.data_dir) / "icons"
    (path / "cache").mkdir(parents=True, exist_ok=True)
    return path


def _sniff(data: bytes, declared: str) -> str | None:
    """Typ obrazu po nagłówku pliku (serwery często podają text/plain dla .ico)."""
    head = data[:16]
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if b"<svg" in data[:512].lower() and declared.startswith("image/svg"):
        return "image/svg+xml"
    return None


class _Links(HTMLParser):
    """Zbiera <link rel="icon|apple-touch-icon" href sizes>."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel, href = a.get("rel", "").lower(), a.get("href", "")
        if not href or "icon" not in rel or "mask-icon" in rel:
            return
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", a.get("sizes", ""))]
        score = max(sizes or [16 if "shortcut" in rel or rel == "icon" else 32])
        if "apple-touch-icon" in rel:
            score = max(score, 180)
        if href.lower().endswith(".svg") or a.get("type", "").startswith("image/svg"):
            score = max(score, 96)
        self.found.append((min(score, 256), href))


def favicon_links(html: str, base: str) -> list[str]:
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — popsuty HTML nie blokuje reszty
        pass
    return [urljoin(base, href) for _, href in sorted(parser.found, key=lambda x: -x[0])]


def split_image(image: str) -> tuple[str, str, str]:
    """`ghcr.io/a/b:1` → ("ghcr.io", "a", "b"); `nginx` → ("docker.io", "library", "nginx")."""
    ref = image.split("@", 1)[0]
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag:
        name = ref
    parts = name.split("/")
    registry = "docker.io"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, parts = parts[0], parts[1:]
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
        if len(parts) == 1:
            parts = ["library", *parts]
    return registry, "/".join(parts[:-1]), parts[-1].lower()


def registry_candidates(image: str) -> list[str]:
    registry, namespace, repo = split_image(image)
    out = []
    if registry == "docker.io":
        out.append(f"https://hub.docker.com/api/media/repos_logo/v1/{quote(namespace + '/' + repo, safe='')}")  # oficjalne i zweryfikowane
    out.append(f"https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/{quote(repo)}.png")
    if registry == "ghcr.io" and namespace:
        out.append(f"https://github.com/{quote(namespace.split('/')[0])}.png?size=128")
    return out


def app_image(panel: Any, app: dict) -> str | None:
    from . import deploy

    try:
        spec, _ = deploy.load(panel, app)
    except Exception:  # noqa: BLE001
        return None
    if spec.source.kind == "image":
        return spec.source.image
    if spec.source.kind == "compose" and spec.source.compose:
        found = re.search(r"^\s*image:\s*['\"]?([^\s'\"#]+)", spec.source.compose, re.M)
        return found.group(1) if found else None
    return None


def cache_key(app: dict, image: str | None) -> str:
    raw = json.dumps([app.get("icon") or "", (app.get("domains") or [""])[:1], image or ""])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def version(app: dict) -> str:
    """Do adresu ikony w HTML — zmienia się po zmianie ikony lub domeny (przeglądarka pobierze nową)."""
    return hashlib.sha1(json.dumps([app.get("icon") or "", (app.get("domains") or [""])[:1]]).encode()).hexdigest()[:8]


async def _public_url(url: str) -> bool:
    """Własny adres ikony nie może celować w sieć wewnętrzną panelu (SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def _get(client: httpx.AsyncClient, url: str, limit: int = MAX_BYTES) -> tuple[bytes, str, str] | None:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    return None
            return bytes(body), response.headers.get("content-type", "").split(";")[0].strip().lower(), str(response.url)
    except (httpx.HTTPError, asyncio.TimeoutError):
        return None


async def _image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    got = await _get(client, url)
    if not got:
        return None
    data, declared, _ = got
    kind = _sniff(data, declared)
    return (data, kind) if kind in FETCH_TYPES and len(data) > 32 else None


async def _favicon(client: httpx.AsyncClient, domain: str) -> tuple[bytes, str] | None:
    for scheme in ("https", "http"):
        page = await _get(client, f"{scheme}://{domain}/", limit=512 * 1024)
        if page is None:
            continue
        data, declared, final = page
        if "html" in declared:
            for link in favicon_links(data.decode("utf-8", errors="replace"), final)[:4]:
                if urlparse(link).hostname != domain and not await _public_url(link):
                    continue  # HTML strony nie może skierować panelu do jego sieci wewnętrznej
                found = await _image(client, link)
                if found:
                    return found
        found = await _image(client, urljoin(final, "/favicon.ico"))
        if found:
            return found
        break
    return None


async def _resolve(panel: Any, app: dict, image: str | None) -> tuple[bytes, str] | None:
    custom = app.get("icon") or ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=3.0), follow_redirects=True, headers=HEADERS) as client:
        if custom.startswith("url:"):
            url = custom[4:]
            if not await _public_url(url):
                return None
            async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=3.0), follow_redirects=False, headers=HEADERS) as strict:
                return await _image(strict, url)
        domain = (app.get("domains") or [None])[0]
        if domain:
            found = await _favicon(client, domain)
            if found:
                return found
        if image:
            for url in registry_candidates(image):
                found = await _image(client, url)
                if found:
                    return found
    return None


def _custom_file(panel: Any, app: dict) -> tuple[bytes, str] | None:
    name = (app.get("icon") or "")[5:]
    if not re.fullmatch(r"[a-z0-9-]+\.(png|jpg|gif|webp|ico)", name):
        return None
    path = icon_dir(panel) / name
    return (path.read_bytes(), EXT_TYPES[name.rsplit(".", 1)[1]]) if path.is_file() else None


async def get(panel: Any, app: dict) -> tuple[bytes, str] | None:
    """(bajty, typ) albo None — z pamięci na dysku, a gdy jej brak lub jest stara, z sieci."""
    if (app.get("icon") or "").startswith("file:"):
        return _custom_file(panel, app)
    image = await asyncio.to_thread(app_image, panel, app)
    key = cache_key(app, image)
    base = icon_dir(panel) / "cache" / str(app["id"])
    meta_path, data_path = base.with_suffix(".json"), base.with_suffix(".bin")
    lock = _locks.setdefault(app["id"], asyncio.Lock())
    async with lock:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        age = time.time() - meta.get("at", 0)
        if meta.get("key") == key and age < (MISS_TTL if meta.get("miss") else TTL):
            if meta.get("miss"):
                return None
            try:
                return data_path.read_bytes(), meta["type"]
            except OSError:
                pass
        found = await _resolve(panel, app, image)
        if found:
            data_path.write_bytes(found[0])
        meta_path.write_text(json.dumps({"key": key, "at": time.time(), "miss": not found, "type": found[1] if found else None}), encoding="utf-8")
        return found


def forget(panel: Any, app_id: int) -> None:
    for suffix in (".json", ".bin"):
        (icon_dir(panel) / "cache" / f"{app_id}{suffix}").unlink(missing_ok=True)


def save_upload(panel: Any, app_id: int, data: bytes, declared: str) -> str:
    """Zapisuje wgraną ikonę; zwraca wartość kolumny `apps.icon`. ValueError przy złym pliku."""
    kind = _sniff(data, declared)
    if len(data) > MAX_BYTES or kind not in UPLOAD_TYPES:
        raise ValueError("bad icon")
    for old in icon_dir(panel).glob(f"custom-{app_id}-*"):
        old.unlink(missing_ok=True)
    name = f"custom-{app_id}-{int(time.time())}.{UPLOAD_TYPES[kind]}"
    (icon_dir(panel) / name).write_bytes(data)
    return f"file:{name}"
