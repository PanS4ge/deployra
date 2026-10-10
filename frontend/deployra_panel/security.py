"""Sesje, CSRF, limit prób logowania i szyfrowanie sekretów w bazie (AES-GCM)."""

from __future__ import annotations

import base64
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import Database, now

SESSION_COOKIE = "dpl_session"


class Sealer:
    """Szyfruje sekrety (tokeny, klucze API) zapisywane w bazie kluczem z pliku `secret.key`."""

    PREFIX = "v1:"

    def __init__(self, key_path: Path) -> None:
        if not key_path.exists():
            key_path.write_bytes(AESGCM.generate_key(bit_length=256))
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        self._aes = AESGCM(key_path.read_bytes())

    def seal(self, value: str | bytes | None) -> str | None:
        if value is None or value == "":
            return None
        raw = value.encode() if isinstance(value, str) else value
        nonce = os.urandom(12)
        return self.PREFIX + base64.b64encode(nonce + self._aes.encrypt(nonce, raw, None)).decode()

    def unseal_bytes(self, value: str | None) -> bytes | None:
        if not value:
            return None
        if not value.startswith(self.PREFIX):
            raise ValueError("nieznany format sekretu")
        blob = base64.b64decode(value[len(self.PREFIX) :])
        return self._aes.decrypt(blob[:12], blob[12:], None)

    def unseal(self, value: str | None) -> str | None:
        raw = self.unseal_bytes(value)
        return raw.decode() if raw is not None else None


class Sessions:
    def __init__(self, db: Database, hours: int) -> None:
        self.db = db
        self.ttl = hours * 3600

    def create(self, user_id: int, ip: str | None, user_agent: str | None) -> tuple[str, str]:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.db.insert(
            "sessions", id=sid, user_id=user_id, csrf=csrf, created_at=now(), expires_at=time.time() + self.ttl,
            ip=ip, user_agent=(user_agent or "")[:300],
        )  # fmt: skip
        return sid, csrf

    def load(self, sid: str | None) -> dict[str, Any] | None:
        if not sid or len(sid) > 100:
            return None
        row = self.db.fetchone(
            "SELECT s.id AS sid, s.csrf, s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            [sid],
        )
        if not row:
            return None
        if row["expires_at"] < time.time():
            self.db.delete("sessions", sid)
            return None
        return row

    def destroy(self, sid: str) -> None:
        self.db.delete("sessions", sid)

    def destroy_for_user(self, user_id: int) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", [user_id])

    def cleanup(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", [time.time()])


class LoginLimiter:
    """Max 10 nieudanych logowań na 15 minut z jednego IP."""

    def __init__(self, limit: int = 10, window: int = 900) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            cutoff = time.time() - self.window
            hits = [t for t in self._hits.get(ip, []) if t > cutoff]
            self._hits[ip] = hits
            return len(hits) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            self._hits.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)
