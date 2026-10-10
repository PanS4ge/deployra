"""Podpisywanie żądań między komponentami Deployry.

Każde żądanie niesie znacznik czasu, jednorazowy nonce i podpis HMAC-SHA256
z (metoda, ścieżka z query, ts, nonce, sha256 body). Odbiorca odrzuca żądania
spoza okna czasowego i powtórki nonce. Zastępuje stare „Diffie-Hellman + XOR”.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping

H_KEY = "X-Deployra-Key"
H_TS = "X-Deployra-Ts"
H_NONCE = "X-Deployra-Nonce"
H_SIG = "X-Deployra-Sig"
H_BODY = "X-Deployra-Body-Sha256"

WINDOW = 60
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class SignatureError(Exception):
    """Żądanie bez poprawnego podpisu."""


def _canonical(method: str, path: str, ts: int, nonce: str, body_sha256: str) -> bytes:
    return "\n".join([method.upper(), path, str(ts), nonce, body_sha256]).encode()


def sign(
    secret: bytes,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    key_id: str = "panel",
    ts: int | None = None,
    nonce: str | None = None,
    body_sha256: str | None = None,
) -> dict[str, str]:
    """Zwraca nagłówki podpisu. `path` musi zawierać query (np. `/api/x?a=1`).

    Dla strumieniowanych uploadów podaj `body_sha256` zamiast `body`; odbiorca
    sprawdzi skrót po odebraniu całości.
    """
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or secrets.token_hex(12)
    digest = body_sha256 or hashlib.sha256(body).hexdigest()
    sig = hmac.new(secret, _canonical(method, path, ts, nonce, digest), hashlib.sha256).hexdigest()
    headers = {H_KEY: key_id, H_TS: str(ts), H_NONCE: nonce, H_SIG: sig}
    if body_sha256:
        headers[H_BODY] = body_sha256
    return headers


class NonceCache:
    """Pamięta nonce z okna czasowego, żeby odrzucać powtórki."""

    def __init__(self, window: int = WINDOW, max_size: int = 50_000) -> None:
        self.window = window
        self.max_size = max_size
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str, now: float) -> bool:
        """True, jeśli nonce jest nowy (i został zapisany)."""
        with self._lock:
            cutoff = now - 2 * self.window
            while self._seen:
                oldest_nonce, oldest_ts = next(iter(self._seen.items()))
                if oldest_ts >= cutoff and len(self._seen) < self.max_size:
                    break
                self._seen.pop(oldest_nonce)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return value


def verify(
    secret: bytes,
    method: str,
    path: str,
    body: bytes | None,
    headers: Mapping[str, str],
    *,
    nonces: NonceCache | None,
    now: float | None = None,
    window: int = WINDOW,
) -> str:
    """Sprawdza podpis i zwraca identyfikator klucza. Rzuca `SignatureError`.

    `body=None` oznacza upload strumieniowy: podpis obejmuje zadeklarowany
    nagłówek `X-Deployra-Body-Sha256`, który wywołujący musi potem porównać
    z faktycznym skrótem odebranych danych.
    """
    key_id = _header(headers, H_KEY)
    ts_raw = _header(headers, H_TS)
    nonce = _header(headers, H_NONCE)
    sig = _header(headers, H_SIG)
    if not (key_id and ts_raw and nonce and sig):
        raise SignatureError("brak nagłówków podpisu")
    try:
        ts = int(ts_raw)
    except ValueError as exc:
        raise SignatureError("zły znacznik czasu") from exc
    now = time.time() if now is None else now
    if abs(now - ts) > window:
        raise SignatureError("znacznik czasu poza oknem (sprawdź zegar/NTP)")
    if body is None:
        digest = _header(headers, H_BODY)
        if not digest:
            raise SignatureError("brak skrótu body")
    else:
        digest = hashlib.sha256(body).hexdigest()
    expected = hmac.new(secret, _canonical(method, path, ts, nonce, digest), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("zły podpis")
    if nonces is not None and not nonces.check_and_add(nonce, now):
        raise SignatureError("powtórzone żądanie")
    return key_id
