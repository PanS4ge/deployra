"""Bezpieczeństwo kont panelu: 2FA (TOTP + kody zapasowe), potwierdzanie tożsamości."""

from __future__ import annotations

import hmac
from typing import Any

from deployra_common import totp
from deployra_common.passwords import hash_password, verify_password


def has_2fa(user: dict | None) -> bool:
    return bool(user and user.get("totp_secret"))


def check_code(panel: Any, user: dict, code: str) -> bool:
    """Kod z aplikacji (bez powtórki tego samego kroku) albo jednorazowy kod zapasowy."""
    secret = panel.sealer.unseal(user.get("totp_secret"))
    step = totp.verify(secret, code, user.get("totp_step"))
    if step is not None:
        panel.db.update("users", user["id"], totp_step=step)
        return True
    return use_backup_code(panel, user, code)


def use_backup_code(panel: Any, user: dict, code: str) -> bool:
    clean = (code or "").strip().upper().replace(" ", "")
    if len(clean) < 8:
        return False
    if "-" not in clean and len(clean) == 8:
        clean = f"{clean[:4]}-{clean[4:]}"
    stored = list(user.get("backup_codes") or [])
    for index, hashed in enumerate(stored):
        if hashed and verify_password(clean, hashed):
            stored[index] = None  # jednorazowy
            panel.db.update("users", user["id"], backup_codes=stored)
            return True
    return False


def new_backup_codes(panel: Any, user_id: int) -> list[str]:
    codes = totp.backup_codes()
    panel.db.update("users", user_id, backup_codes=[hash_password(c) for c in codes])
    return codes


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
