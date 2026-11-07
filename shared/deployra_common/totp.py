"""Kody jednorazowe TOTP (RFC 6238): HMAC-SHA1, 6 cyfr, krok 30 s — zgodne z Aegis, Google Authenticator, 1Password.

Sekret to 20 losowych bajtów w base32. `verify` przyjmuje kod z bieżącego kroku ±1 (rozjechany zegar telefonu) i zwraca
numer kroku, żeby wywołujący mógł odrzucić drugie użycie tego samego kodu (`last_step`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP, DIGITS, DRIFT = 30, 6, 1


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    clean = secret.strip().replace(" ", "").upper()
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def now_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // STEP)


def verify(secret: str | None, code: str, last_step: int | None = None, at: float | None = None) -> int | None:
    """Numer kroku pasującego kodu albo None. Kod z kroku ≤ `last_step` (już użyty) jest odrzucany."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != DIGITS:
        return None
    try:
        current = now_step(at)
        for step in range(current - DRIFT, current + DRIFT + 1):
            if (last_step is None or step > last_step) and hmac.compare_digest(code_at(secret, step), code):
                return step
    except (ValueError, TypeError):
        return None
    return None


def uri(secret: str, account: str, issuer: str = "Deployra") -> str:
    """Adres otpauth:// — telefon otwiera go w aplikacji uwierzytelniającej (albo z kodu QR)."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP})


def backup_codes(count: int = 10) -> list[str]:
    """Kody zapasowe (jednorazowe), np. `7KQ2-M9XD`."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)) for _ in range(count)]
