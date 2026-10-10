"""Konta SFTP w stanie węzła (bez importu asyncssh — ten ładujemy dopiero przy starcie serwera)."""

from __future__ import annotations

import re
from typing import Any

from deployra_common.passwords import hash_password
from deployra_common.schemas import check_slug

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")


def check_username(username: str) -> str:
    if not _USERNAME.match(username):
        raise ValueError("login SFTP: małe litery, cyfry, . _ - (2–32 znaki)")
    return username


def set_user(state: Any, username: str, slug: str, password: str | None, keys: list[str] | None) -> dict:
    check_username(username)
    check_slug(slug)
    for key in keys or []:
        import asyncssh  # walidacja formatu klucza

        asyncssh.import_public_key(key)
    if not password and not keys:
        raise ValueError("podaj hasło albo klucz publiczny")

    def change(users: dict) -> dict:
        current = users.get(username, {})
        users[username] = {
            "slug": slug,
            "hash": hash_password(password) if password else current.get("hash"),
            "keys": keys if keys is not None else current.get("keys", []),
        }
        return users

    users = state.mutate("sftp_users", change)
    return {"username": username, "slug": users[username]["slug"], "keys": len(users[username]["keys"])}


def delete_user(state: Any, username: str) -> None:
    state.mutate("sftp_users", lambda users: {k: v for k, v in users.items() if k != username})


def list_users(state: Any) -> list[dict]:
    return [
        {"username": name, "slug": info["slug"], "password": bool(info.get("hash")), "keys": len(info.get("keys", []))}
        for name, info in sorted(state.get("sftp_users", {}).items())
    ]
