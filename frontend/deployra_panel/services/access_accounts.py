"""Konta list dostępu (bez kont panelu) i sesje „logowania Deployra” na hostach aplikacji.

Konto należy do jednej listy. Hasło trzymamy dwa razy: scrypt (logowanie Deployra) i apr1 (okienko nginx — tylko ten
format rozumie `auth_basic_user_file`), więc przełączenie trybu listy nie wymaga nowych haseł. Opcjonalnie TOTP.

Sesja logowania Deployra to losowy token w ciasteczku hosta aplikacji; w bazie leży tylko jego skrót. Wyłączenie
albo usunięcie konta, zmiana hasła i przełączenie listy kończą sesje od razu (w odróżnieniu od podpisanych ciasteczek).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any

from deployra_common import totp
from deployra_common.passwords import apr1, dummy_hash, hash_password, verify_password

from ..db import now
from ..i18n import t

USERNAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
MODES = ("none", "deployra", "basic")
LOCK_AFTER, LOCK_SECONDS = 10, 900
DEFAULT_SESSION_HOURS = 12


def panel_user_allowed(acl: dict, user: dict) -> bool:
    """Konto panelu na hoście z logowaniem Deployra: lista musi wpuszczać konta panelu, a gdy wybrano konkretne —
    to musi być jedno z nich. Konta „tylko przypisane serwery” nigdy."""
    if not acl.get("allow_panel", 1) or user.get("scoped"):
        return False
    allowed = [int(u) for u in acl.get("login_users") or []]
    return not allowed or int(user["id"]) in allowed


def mode_of(acl: dict | None) -> str:
    if not acl:
        return "none"
    mode = acl.get("login_mode") or "none"
    if mode == "none" and acl.get("deployra_login"):  # wiersz sprzed trybów albo zapisany starszym kodem
        mode = "deployra"
    elif mode == "none" and acl.get("users"):  # stare hasła Basic jeszcze nieprzeniesione do kont
        mode = "basic"
    return mode if mode in MODES else "none"


# --- konta --------------------------------------------------------------------------------------


def accounts(panel: Any, acl_id: int) -> list[dict]:
    return panel.db.fetchall("SELECT * FROM access_accounts WHERE acl_id = ? ORDER BY username", [acl_id])


def _check_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError(t("msg.password_short"))


def create(panel: Any, acl_id: int, username: str, password: str, name: str = "") -> int:
    username = username.strip()
    if not USERNAME.match(username):
        raise ValueError(t("access.bad_username"))
    _check_password(password)
    if panel.db.fetchone("SELECT id FROM access_accounts WHERE acl_id = ? AND username = ?", [acl_id, username]):
        raise ValueError(t("access.account_exists", username=username))
    return panel.db.insert(
        "access_accounts", acl_id=acl_id, username=username, name=name.strip()[:60], password_hash=hash_password(password),
        htpasswd=apr1(password), enabled=1, failed=0, created_at=now(),
    )  # fmt: skip


def set_password(panel: Any, account_id: int, password: str) -> None:
    _check_password(password)
    panel.db.update("access_accounts", account_id, password_hash=hash_password(password), htpasswd=apr1(password), failed=0, locked_until=None)
    revoke_account(panel, account_id)


def set_enabled(panel: Any, account_id: int, enabled: bool) -> None:
    panel.db.update("access_accounts", account_id, enabled=int(enabled), failed=0, locked_until=None)
    if not enabled:
        revoke_account(panel, account_id)


def delete(panel: Any, account_id: int) -> None:
    revoke_account(panel, account_id)
    panel.db.delete("access_accounts", account_id)


def set_totp(panel: Any, account_id: int, secret: str | None) -> None:
    panel.db.update("access_accounts", account_id, totp_secret=panel.sealer.seal(secret) if secret else None, totp_step=None)
    revoke_account(panel, account_id)


def htpasswd_users(panel: Any, acl_id: int) -> list[dict]:
    """Konta do pliku htpasswd (tryb okienka nginx): tylko włączone i z hasłem apr1."""
    return [{"username": a["username"], "htpasswd": a["htpasswd"]} for a in accounts(panel, acl_id) if a["enabled"] and a.get("htpasswd")]


def migrate_legacy(panel: Any) -> int:
    """Stare wpisy `users` list dostępu (login + apr1) → konta listy. Hasło scrypt powstanie przy pierwszym logowaniu."""
    moved = 0
    for acl in panel.db.fetchall("SELECT id, users FROM access_lists"):
        users = acl.get("users") or []
        if not users:
            continue
        for user in users:
            username, htpasswd = str(user.get("username") or ""), str(user.get("htpasswd") or "")
            if USERNAME.match(username) and htpasswd and not panel.db.fetchone(
                "SELECT id FROM access_accounts WHERE acl_id = ? AND username = ?", [acl["id"], username]
            ):
                panel.db.insert("access_accounts", acl_id=acl["id"], username=username, name="", password_hash=None, htpasswd=htpasswd,
                                enabled=1, failed=0, created_at=now())  # fmt: skip
                moved += 1
        panel.db.update("access_lists", acl["id"], users=[])
    return moved


def _apr1_matches(password: str, stored: str | None) -> bool:
    parts = (stored or "").split("$")
    if len(parts) != 4 or parts[1] != "apr1":
        return False
    return hmac.compare_digest(apr1(password, parts[2]), stored or "")


def authenticate(panel: Any, acl_id: int, username: str, password: str, ip: str) -> tuple[dict | None, str]:
    """(konto, powód błędu). Nieistniejący login też liczy hash (czas odpowiedzi nie zdradza kont); po LOCK_AFTER
    błędach konto jest zablokowane na LOCK_SECONDS — także dla poprawnego hasła."""
    account = panel.db.fetchone("SELECT * FROM access_accounts WHERE acl_id = ? AND username = ?", [acl_id, (username or "").strip()])
    if not account:
        verify_password(password, dummy_hash())
        return None, "bad"
    if account.get("locked_until") and account["locked_until"] > time.time():
        verify_password(password, dummy_hash())
        return None, "locked"
    if account.get("password_hash"):
        ok = verify_password(password, account["password_hash"])
    else:  # konto przeniesione ze starej listy: tylko apr1
        verify_password(password, dummy_hash())
        ok = _apr1_matches(password, account.get("htpasswd"))
    if not ok or not account["enabled"]:
        failed = int(account.get("failed") or 0) + 1
        if failed >= LOCK_AFTER:
            panel.db.update("access_accounts", account["id"], failed=0, locked_until=time.time() + LOCK_SECONDS)
            return None, "locked_now"
        panel.db.update("access_accounts", account["id"], failed=failed)
        return None, "bad"
    values: dict[str, Any] = {"failed": 0, "locked_until": None}
    if not account.get("password_hash"):
        values.update(password_hash=hash_password(password))
    panel.db.update("access_accounts", account["id"], **values)
    return {**account, **values}, ""


def check_totp(panel: Any, account: dict, code: str) -> bool:
    secret = panel.sealer.unseal(account.get("totp_secret"))
    step = totp.verify(secret, code, account.get("totp_step"))
    if step is None:
        return False
    panel.db.update("access_accounts", account["id"], totp_step=step)  # ten sam kod drugi raz nie przejdzie
    return True


def logged_in(panel: Any, account_id: int, ip: str) -> None:
    panel.db.update("access_accounts", account_id, last_login_at=time.time(), last_login_ip=ip[:64])


# --- sesje ---------------------------------------------------------------------------------------


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_session(panel: Any, acl: dict, host: str, ip: str, user_agent: str | None, account_id: int | None = None,
                   user_id: int | None = None) -> str:  # fmt: skip
    raw = secrets.token_urlsafe(32)
    hours = int(acl.get("session_hours") or DEFAULT_SESSION_HOURS)
    stamp = time.time()
    panel.db.insert(
        "forward_sessions", id=_hash(raw), acl_id=acl["id"], account_id=account_id, user_id=user_id, host=host, created_at=stamp,
        expires_at=stamp + hours * 3600, last_seen=stamp, ip=ip[:64], user_agent=(user_agent or "")[:300],
    )  # fmt: skip
    if secrets.randbelow(50) == 0:
        cleanup(panel)
    return raw


def session(panel: Any, raw: str | None, host: str) -> dict | None:
    """Ważna sesja dla tego hosta, z nazwą do nagłówka X-Deployra-User; None, gdy wygasła, inny host albo konto
    straciło dostęp (wyłączone, usunięte, lista zmieniła tryb)."""
    if not raw or len(raw) > 200:
        return None
    row = panel.db.fetchone("SELECT * FROM forward_sessions WHERE id = ?", [_hash(raw)])
    if not row or row["host"] != host or row["expires_at"] < time.time():
        return None
    acl = panel.db.get("access_lists", row["acl_id"])
    if mode_of(acl) != "deployra":
        return None
    if row.get("account_id"):
        account = panel.db.get("access_accounts", row["account_id"])
        if not account or not account["enabled"] or account["acl_id"] != acl["id"]:
            return None
        who = account["username"]
    else:
        user = panel.db.get("users", row.get("user_id") or 0)
        if not user or not panel_user_allowed(acl, user):
            return None
        who = user["email"]
    if time.time() - (row.get("last_seen") or 0) > 300:  # ostatnia aktywność, bez zapisu przy każdym żądaniu
        panel.db.execute("UPDATE forward_sessions SET last_seen = ? WHERE id = ?", [time.time(), row["id"]])
    return {**row, "who": who}


def revoke(panel: Any, raw: str | None) -> None:
    if raw:
        panel.db.execute("DELETE FROM forward_sessions WHERE id = ?", [_hash(raw)])


def revoke_account(panel: Any, account_id: int) -> None:
    panel.db.execute("DELETE FROM forward_sessions WHERE account_id = ?", [account_id])


def revoke_acl(panel: Any, acl_id: int) -> None:
    panel.db.execute("DELETE FROM forward_sessions WHERE acl_id = ?", [acl_id])


def sessions_of(panel: Any, acl_id: int) -> list[dict]:
    return panel.db.fetchall("SELECT * FROM forward_sessions WHERE acl_id = ? AND expires_at > ? ORDER BY last_seen DESC", [acl_id, time.time()])


def cleanup(panel: Any) -> None:
    panel.db.execute("DELETE FROM forward_sessions WHERE expires_at < ?", [time.time()])


def gw_token(panel: Any) -> str:
    """Token bramki dla endpointów logowania na hostach aplikacji (wyprowadzony z klucza panelu, stały)."""
    return hmac.new(panel.sealer.derive("forward-gw"), b"v1", hashlib.sha256).hexdigest()[:48]


def gw_ok(panel: Any, value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value.encode(), gw_token(panel).encode())
