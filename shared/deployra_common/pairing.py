"""Synchronizacja (parowanie) komponentów: X25519 + HKDF, uwierzytelnione kodem.

To jest poprawna wersja dawnego „Diffie-Hellmana”: obie strony wysyłają
efemeryczne klucze publiczne X25519, każdą wiadomość pieczętują HMAC-iem
z jednorazowego kodu (lub sekretu z tokenu dołączenia), a wspólny sekret API
wylicza HKDF. Atakujący w środku bez kodu nie podmieni kluczy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .i18n import tr

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # bez 0/O, 1/I/L, U


def new_keypair() -> tuple[str, str]:
    """Para X25519 (prywatny, publiczny) w base64."""
    key = X25519PrivateKey.generate()
    return (
        base64.b64encode(key.private_bytes_raw()).decode(),
        base64.b64encode(key.public_key().public_bytes_raw()).decode(),
    )


def _decode_key(value: str) -> bytes:
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError(tr("srv.pair.key_32"))
    return raw


def derive_secret(my_private_b64: str, peer_public_b64: str, psk: str, context: str) -> bytes:
    """Wspólny 32-bajtowy sekret API: HKDF(X25519(my, peer), salt=sha256(psk))."""
    private = X25519PrivateKey.from_private_bytes(_decode_key(my_private_b64))
    shared = private.exchange(X25519PublicKey.from_public_bytes(_decode_key(peer_public_b64)))
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(psk.encode()).digest(),
        info=b"deployra/v1/" + context.encode(),
    ).derive(shared)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {k: v for k, v in payload.items() if k != "mac"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def seal(psk: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dodaje do wiadomości pole `mac` (HMAC-SHA256 kluczem z psk). Pole `kind` rozróżnia etapy."""
    if "kind" not in payload:
        raise ValueError(tr("srv.pair.no_kind"))
    key = hashlib.sha256(psk.encode()).digest()
    return {**payload, "mac": hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()}


def unseal(psk: str, payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Sprawdza pieczęć i rodzaj wiadomości. Rzuca `ValueError`."""
    mac = payload.get("mac")
    if not isinstance(mac, str) or payload.get("kind") != kind:
        raise ValueError(tr("srv.pair.bad_message"))
    key = hashlib.sha256(psk.encode()).digest()
    expected = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError(tr("srv.pair.bad_code"))
    return {k: v for k, v in payload.items() if k != "mac"}


def new_code(groups: int = 3, size: int = 4) -> str:
    """Kod parowania, np. `DPLR-7KQ2-MX9P-R4TZ` (~59 bitów)."""
    parts = ["".join(secrets.choice(_CODE_ALPHABET) for _ in range(size)) for _ in range(groups)]
    return "DPLR-" + "-".join(parts)


def normalize_code(code: str) -> str:
    code = code.strip().upper().replace(" ", "")
    if not code.startswith("DPLR-"):
        code = "DPLR-" + code
    return code


@dataclass(frozen=True)
class JoinToken:
    """Token dołączenia węzła: `dplr1.<id>.<join>.<pair>`.

    `join` zna bramka (dodaje peer WG), `pair` tylko panel i węzeł —
    dzięki temu bramka nie pozna sekretu API panel↔węzeł.
    """

    id: str
    join_secret: str
    pair_secret: str

    PREFIX = "dplr1"

    @classmethod
    def generate(cls) -> "JoinToken":
        return cls(secrets.token_hex(6), secrets.token_urlsafe(18), secrets.token_urlsafe(18))

    def encode(self) -> str:
        return ".".join([self.PREFIX, self.id, self.join_secret, self.pair_secret])

    @classmethod
    def parse(cls, value: str) -> "JoinToken":
        parts = value.strip().split(".")
        if len(parts) != 4 or parts[0] != cls.PREFIX or not all(parts[1:]):
            raise ValueError(tr("srv.pair.bad_token"))
        return cls(parts[1], parts[2], parts[3])
