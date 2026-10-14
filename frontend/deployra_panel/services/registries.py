"""Zarządzanie prywatnymi rejestrami Docker (Docker Hub, GHCR, GitLab, Harbor itp.)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..context import Panel
from ..db import now


def list_registries(panel: Panel) -> list[dict[str, Any]]:
    return panel.db.fetchall("SELECT id, name, url, username, created_at FROM registries ORDER BY name")


def add_registry(panel: Panel, name: str, url: str, username: str, password: str) -> int:
    name = name.strip()
    url = normalize_registry_url(url)
    username = username.strip()
    password = password.strip()
    if not name or not url or not username or not password:
        raise ValueError("Wszystkie pola są wymagane")

    sealed_pwd = panel.sealer.seal(password)
    return panel.db.insert(
        "registries",
        name=name,
        url=url,
        username=username,
        password=sealed_pwd,
        created_at=now(),
    )


def delete_registry(panel: Panel, reg_id: int) -> None:
    panel.db.execute("DELETE FROM registries WHERE id = ?", [reg_id])


def normalize_registry_url(url: str) -> str:
    """Normalizuje URL/domenę rejestru (np. 'https://ghcr.io/' -> 'ghcr.io')."""
    clean = url.strip()
    if "://" in clean:
        clean = urlparse(clean).netloc or urlparse(clean).path
    return clean.strip("/")


def auth_for_image(panel: Panel, image: str) -> dict[str, str] | None:
    """Wyszukuje poświadczenia logowania do rejestru pasujące do zadanego obrazu."""
    image = image.strip()
    parts = image.split("/", 1)
    if len(parts) == 1:
        reg_host = "docker.io"
    else:
        first = parts[0]
        if "." in first or ":" in first or first == "localhost":
            reg_host = first
        else:
            reg_host = "docker.io"

    rows = panel.db.fetchall("SELECT * FROM registries")
    for r in rows:
        r_url = normalize_registry_url(r["url"])
        if r_url.lower() == reg_host.lower() or (
            reg_host in ("docker.io", "index.docker.io") and r_url.lower() in ("docker.io", "index.docker.io")
        ):
            pwd = panel.sealer.unseal(r["password"]) if r.get("password") else ""
            return {
                "username": r["username"],
                "password": pwd,
                "serveraddress": r["url"],
            }
    return None
