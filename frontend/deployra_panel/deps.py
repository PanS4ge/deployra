"""Zależności FastAPI: zalogowany użytkownik i uprawnienia."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, Request

from .context import Panel, get_panel
from .i18n import t
from .permissions import Permission, has


class LoginRequired(Exception):
    """Brak sesji — przekierowanie na /login."""


class Forbidden(Exception):
    def __init__(self, message: str | None = None) -> None:
        message = message or t("msg.forbidden")
        super().__init__(message)
        self.message = message


def current_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        raise LoginRequired()
    return user


def require(*perms: Permission) -> Callable[..., dict[str, Any]]:
    def dependency(user: dict = Depends(current_user)) -> dict[str, Any]:
        if not all(has(user, perm) for perm in perms):
            raise Forbidden()
        return user

    return dependency


def node_or_404(node_id: int, panel: Panel = Depends(get_panel)) -> dict[str, Any]:
    node = panel.node(node_id)
    if not node:
        raise HTTPException(404, t("msg.no_node"))
    return node
