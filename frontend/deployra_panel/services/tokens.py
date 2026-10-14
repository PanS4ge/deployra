"""Zarządzanie tokenami API / MCP dla agentów AI i integracji zewnętrznych."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import Panel

TOKEN_PREFIX = "dpl_mcp_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_token(panel: Panel, name: str, user_id: int, permissions: str = "") -> tuple[str, dict[str, Any]]:
    """Tworzy nowy token API i zapisuje jego hash w bazie. Zwraca surowy token (jednorazowo widoczny) i wiersz bazy."""
    raw = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    thash = hash_token(raw)
    prefix = f"{raw[:14]}..."
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S")

    row_id = panel.db.insert(
        "api_tokens",
        name=name.strip()[:60] or "MCP Agent",
        token_hash=thash,
        prefix=prefix,
        user_id=user_id,
        permissions=permissions.strip(),
        created_at=now_iso,
        last_used_at=None,
    )
    row = panel.db.get("api_tokens", row_id)
    return raw, row


def verify_token(panel: Panel, raw: str) -> dict[str, Any] | None:
    """Weryfikuje surowy token i zwraca słownik użytkownika powiązanego z tokenem."""
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    thash = hash_token(raw)
    row = panel.db.fetchone("SELECT * FROM api_tokens WHERE token_hash = ?", [thash])
    if not row:
        return None

    user = panel.db.get("users", row["user_id"])
    if not user:
        return None

    # Zapisujemy czas użycia (bez blokowania)
    try:
        panel.db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", [time.time(), row["id"]])
    except Exception:
        pass

    # Klonujemy słownik użytkownika i dodajemy metadane tokena
    auth_user = dict(user)
    if row.get("permissions"):
        auth_user["permissions"] = row["permissions"]
    auth_user["token_id"] = row["id"]
    auth_user["token_name"] = row["name"]
    return auth_user


def list_tokens(panel: Panel, user_id: int | None = None) -> list[dict[str, Any]]:
    """Zwraca listę tokenów z danymi twórcy."""
    if user_id:
        query = "SELECT t.*, u.email as user_email FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id WHERE t.user_id = ? ORDER BY t.id DESC"
        return panel.db.fetchall(query, [user_id])
    query = "SELECT t.*, u.email as user_email FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id ORDER BY t.id DESC"
    return panel.db.fetchall(query)


def delete_token(panel: Panel, token_id: int, user_id: int | None = None) -> bool:
    """Usuwa token API."""
    if user_id:
        return panel.db.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", [token_id, user_id]) > 0
    return panel.db.execute("DELETE FROM api_tokens WHERE id = ?", [token_id]) > 0
