"""Hasła: scrypt (stdlib) dla użytkowników/SFTP oraz apr1 dla htpasswd nginx."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(digest))
        )
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


_APR_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> str:
    out = ""
    for _ in range(length):
        out += _APR_ALPHABET[value & 0x3F]
        value >>= 6
    return out


def apr1(password: str, salt: str | None = None) -> str:
    """Hash `$apr1$` (Apache MD5) — format rozumiany natywnie przez nginx `auth_basic_user_file`."""
    salt = (salt or "".join(secrets.choice(_APR_ALPHABET) for _ in range(8)))[:8]
    pw, magic, sl = password.encode(), b"$apr1$", salt.encode()
    final = hashlib.md5(pw + sl + pw).digest()
    ctx = pw + magic + sl
    for i in range(len(pw), 0, -16):
        ctx += final[: min(16, i)]
    i = len(pw)
    while i:
        ctx += b"\x00" if i & 1 else pw[:1]
        i >>= 1
    final = hashlib.md5(ctx).digest()
    for i in range(1000):
        step = (pw if i & 1 else final) + (sl if i % 3 else b"") + (pw if i % 7 else b"") + (final if i & 1 else pw)
        final = hashlib.md5(step).digest()
    f = final
    encoded = (
        _to64((f[0] << 16) | (f[6] << 8) | f[12], 4)
        + _to64((f[1] << 16) | (f[7] << 8) | f[13], 4)
        + _to64((f[2] << 16) | (f[8] << 8) | f[14], 4)
        + _to64((f[3] << 16) | (f[9] << 8) | f[15], 4)
        + _to64((f[4] << 16) | (f[10] << 8) | f[5], 4)
        + _to64(f[11], 2)
    )
    return f"$apr1${salt}${encoded}"
