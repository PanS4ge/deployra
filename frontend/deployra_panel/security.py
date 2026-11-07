"""Sesje, CSRF, limit prób logowania i szyfrowanie sekretów w bazie (AES-GCM)."""

from __future__ import annotations

import base64
import hashlib
import hmac
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
        self._key = key_path.read_bytes()
        self._aes = AESGCM(self._key)

    def derive(self, purpose: str) -> bytes:
        """Osobny klucz do podpisów (HMAC) dla danego celu — nie ten sam co do szyfrowania sekretów."""
        return hmac.new(self._key, b"deployra:" + purpose.encode(), hashlib.sha256).digest()

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
            "SELECT s.id AS sid, s.csrf, s.expires_at, s.reauth_at, s.last_seen, u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            [sid],
        )
        if not row:
            return None
        if row["expires_at"] < time.time():
            self.db.delete("sessions", sid)
            return None
        if time.time() - float(row.get("last_seen") or 0) > 300:  # ostatnia aktywność (co 5 min, nie przy każdym żądaniu)
            self.db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", [time.time(), sid])
        return row

    def of_user(self, user_id: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY last_seen DESC", [user_id, time.time()])
        return [{**r, "handle": handle(r["id"])} for r in rows]

    def destroy_by_handle(self, user_id: int, value: str) -> bool:
        """Wylogowanie jednej sesji z listy — po skrócie, bo prawdziwy identyfikator sesji nie trafia do przeglądarki."""
        for row in self.db.fetchall("SELECT id FROM sessions WHERE user_id = ?", [user_id]):
            if hmac.compare_digest(handle(row["id"]), value or ""):
                self.db.delete("sessions", row["id"])
                return True
        return False

    def destroy(self, sid: str) -> None:
        self.db.delete("sessions", sid)

    def mark_reauth(self, sid: str) -> None:
        """Tożsamość potwierdzona przed chwilą (hasło / 2FA) — na kilka minut wolno ryzykowne akcje (np. WebSSH)."""
        self.db.execute("UPDATE sessions SET reauth_at = ? WHERE id = ?", [time.time(), sid])

    @staticmethod
    def reauth_fresh(session: dict | None, seconds: int) -> bool:
        return bool(session and session.get("reauth_at") and time.time() - float(session["reauth_at"]) < seconds)

    def destroy_for_user(self, user_id: int) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", [user_id])

    def destroy_others(self, user_id: int, keep: str | None) -> None:
        """Wszystkie sesje konta poza bieżącą (po zmianie własnego hasła)."""
        self.db.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", [user_id, keep or ""])

    def cleanup(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", [time.time()])


def handle(sid: str) -> str:
    return hashlib.sha256(("session:" + sid).encode()).hexdigest()[:20]


class LoginLimiter:
    """Nieudane logowania: z jednego IP max `limit` na `window` sekund (twarda granica), a dla jednego konta rosnące
    opóźnienie po `free` błędach — bez twardej blokady, żeby obcy nie mógł zablokować komuś konta.

    Z bazą (`db`) próby przeżywają restart panelu; bez niej (testy, narzędzia) są w pamięci. `scope` oddziela liczniki
    (panel, logowanie list dostępu)."""

    MAX_DELAY = 10.0

    def __init__(self, db: Database | None = None, limit: int = 10, window: int = 900, free: int = 5, scope: str = "panel") -> None:
        self.db, self.limit, self.window, self.free, self.scope = db, limit, window, free, scope
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _key(self, kind: str, value: str) -> str:
        return f"{self.scope}:{kind}:{(value or '?').strip().lower()[:150]}"

    def _count(self, key: str) -> int:
        cutoff = time.time() - self.window
        if self.db is not None:
            row = self.db.fetchone("SELECT COUNT(*) AS n FROM login_attempts WHERE akey = ? AND at > ?", [key, cutoff])
            return int((row or {}).get("n") or 0)
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > cutoff]
            self._hits[key] = hits
            return len(hits)

    def blocked(self, ip: str) -> bool:
        return self._count(self._key("ip", ip)) >= self.limit

    def delay(self, account: str | None) -> float:
        """Sekundy do odczekania przed sprawdzeniem hasła tego konta (0 dla pierwszych `free` błędów)."""
        if not account:
            return 0.0
        over = self._count(self._key("acct", account)) - self.free
        return 0.0 if over < 0 else min(self.MAX_DELAY, 0.5 * 2**over)

    def fail(self, ip: str, account: str | None = None) -> None:
        keys = [self._key("ip", ip)] + ([self._key("acct", account)] if account else [])
        now_ = time.time()
        if self.db is not None:
            for key in keys:
                self.db.insert("login_attempts", akey=key, at=now_)
            self.db.execute("DELETE FROM login_attempts WHERE at < ?", [now_ - 2 * self.window])
            return
        with self._lock:
            for key in keys:
                self._hits.setdefault(key, []).append(now_)

    def reset(self, ip: str, account: str | None = None) -> None:
        keys = [self._key("ip", ip)] + ([self._key("acct", account)] if account else [])
        if self.db is not None:
            for key in keys:
                self.db.execute("DELETE FROM login_attempts WHERE akey = ?", [key])
            return
        with self._lock:
            for key in keys:
                self._hits.pop(key, None)


def client_ip(request: Any, trusted: set[str]) -> str:
    """Adres klienta. Gdy połączenie przyszło od zaufanego proxy (bramka w tunelu, localhost), bierzemy z
    X-Forwarded-For najbardziej prawy adres, który nie jest zaufanym proxy — klient może dopisać swoje wpisy na
    początku nagłówka, ale nie na końcu (dopisuje go nginx)."""
    peer = request.client.host if request.client else "?"
    if peer not in trusted:
        return peer
    hops = [h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",") if h.strip()]
    for hop in reversed(hops):
        if hop not in trusted:
            return hop
    return peer
