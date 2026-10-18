"""Logowanie Deployra przed aplikacją (forward auth przez `auth_request` w nginx).

Przepływ:
1. Przeglądarka wchodzi na host z listą dostępu „wymagaj logowania”. Bramka pyta panel (`/auth/forward`);
   bez ważnego ciasteczka dostaje 401 i trafia na `/login?next=/auth/forward/start?rd=<adres>`.
2. `/auth/forward/start` (zalogowany) wystawia jednorazowy token (60 s) i odsyła na
   `https://<host>/__deployra_auth/callback?token=…`. Bramka przekazuje to do panelu.
3. `/auth/forward/callback` sprawdza token i ustawia na hoście aplikacji ciasteczko `dpl_fwd`
   (HttpOnly, Secure, SameSite=Lax, podpisane: konto, host, wygaśnięcie), potem przekierowuje do `rd`.
Ciasteczko sesji panelu (`dpl_session`) nigdy nie trafia na hosty aplikacji.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from deployra_common.schemas import PanelAuth

FORWARD_COOKIE = "dpl_fwd"
TOKEN_TTL = 60  # sekund; token jest jednorazowy
VERIFY_PATH = "/auth/forward"  # bramka → panel
CALLBACK_PATH = "/auth/forward/callback"  # bramka → panel
START_PATH = "/auth/forward/start"
GATEWAY_CALLBACK = "/__deployra_auth/callback"  # adres na hoście aplikacji (proxy bramki do CALLBACK_PATH)
_PATH = re.compile(r"^/[^\s\\]*$")  # ścieżka powrotu: bez spacji i backslashy

_used: dict[str, float] = {}  # zużyte tokeny: nonce → wygaśnięcie (w pamięci; restart kasuje listę)
_used_lock = threading.Lock()


def hostname(value: str | None) -> str:
    """Nazwa hosta bez portu, małymi literami (z nagłówka Host albo X-Deployra-Host)."""
    try:
        return (urlsplit("//" + (value or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def parse_rd(rd: str) -> tuple[str, str] | None:
    """Adres powrotu → (host, ścieżka). Tylko http(s), bez użytkownika i portu, ścieżka zaczyna się od „/”."""
    if not rd or len(rd) > 2048:
        return None
    try:
        parts = urlsplit(rd)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or parts.netloc.lower() != host:
        return None
    path = parts.path or "/"
    if not _PATH.match(path) or path.startswith("//"):
        return None
    return host, path


def _site_acl(panel: Any, host: str) -> dict[str, Any] | None:
    """Lista dostępu z logowaniem Deployra, do której należy host (tylko włączone hosty proxy)."""
    if not host:
        return None
    for site in panel.db.fetchall("SELECT * FROM sites WHERE kind = 'proxy' AND enabled = 1 AND access_list_id IS NOT NULL"):
        if host in {str(d).lower() for d in site.get("domains") or []}:
            acl = panel.db.get("access_lists", site["access_list_id"])
            return acl if acl and acl.get("deployra_login") else None
    return None


def authorize(panel: Any, host: str, user_id: int | None) -> dict[str, Any] | None:
    """Konto może wejść na host: istnieje i (lista dozwolonych jest pusta albo je zawiera)."""
    if user_id is None:
        return None
    acl = _site_acl(panel, host)
    if not acl:
        return None
    user = panel.db.get("users", user_id)
    if not user:
        return None
    allowed = [int(u) for u in acl.get("login_users") or []]
    if allowed and int(user_id) not in allowed:
        return None
    return user


# --- podpisane wartości (ciasteczko, token) ---------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(panel: Any, kind: str, payload: str) -> str:
    key = panel.sealer.derive(f"forward-{kind}")
    return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def _pack(panel: Any, kind: str, data: dict[str, Any]) -> str:
    payload = _b64(json.dumps(data, separators=(",", ":")).encode())
    return f"{payload}.{_sign(panel, kind, payload)}"


def _unpack(panel: Any, kind: str, value: str | None) -> dict[str, Any] | None:
    """Sprawdza podpis (stała czasowo) i wygaśnięcie; zwraca dane albo None."""
    if not value or len(value) > 2048 or value.count(".") != 1:
        return None
    payload, signature = value.split(".")
    expected = _sign(panel, kind, payload)
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return None
    try:
        data = json.loads(_b64_decode(payload))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("e"), (int, float)) or data["e"] < time.time():
        return None
    return data


def make_cookie(panel: Any, user_id: int, host: str) -> str:
    return _pack(panel, "cookie", {"u": user_id, "h": host, "e": time.time() + panel.config.session_hours * 3600})


def cookie_user(panel: Any, value: str | None, host: str) -> int | None:
    """Id konta z ważnego ciasteczka wystawionego dla tego hosta."""
    data = _unpack(panel, "cookie", value)
    if not data or data.get("h") != host or not isinstance(data.get("u"), int):
        return None
    return data["u"]


def make_token(panel: Any, user_id: int, host: str, path: str) -> str:
    return _pack(panel, "token", {"u": user_id, "h": host, "p": path, "n": secrets.token_urlsafe(16), "e": time.time() + TOKEN_TTL})


def consume_token(panel: Any, value: str | None, host: str) -> tuple[int, str] | None:
    """Jednorazowy token → (id konta, ścieżka powrotu). Drugie użycie albo inny host → None."""
    data = _unpack(panel, "token", value)
    if not data or data.get("h") != host or not isinstance(data.get("u"), int) or not isinstance(data.get("p"), str):
        return None
    nonce = str(data.get("n") or "")
    if not nonce:
        return None
    now = time.time()
    with _used_lock:
        for old in [n for n, exp in _used.items() if exp < now]:
            del _used[old]
        if nonce in _used:
            return None
        _used[nonce] = float(data["e"])
    return data["u"], data["p"]


def start(panel: Any, user_id: int, rd: str) -> str | None:
    """Adres callbacku z tokenem dla zalogowanego konta; None, gdy rd jest złe albo konto nie ma dostępu."""
    target = parse_rd(rd)
    if not target:
        return None
    host, path = target
    if not authorize(panel, host, user_id):
        return None
    return f"https://{host}{GATEWAY_CALLBACK}?token={make_token(panel, user_id, host, path)}"


def panel_auth(panel: Any) -> PanelAuth | None:
    """Adresy panelu dla bramki: przez tunel (adres panelu w hubie WireGuard), bez tunelu — 127.0.0.1."""
    domain = str(panel.db.setting("panel_domain") or "").strip().lower()
    if not domain:
        return None
    hub = panel.gateway() or {}
    address = str(hub.get("panel_address") or "").split("/")[0]
    if not address:
        edge = panel.edge_row()
        address = str(hub.get("hub_address") or "127.0.0.1") if edge and edge.get("secret") else "127.0.0.1"
    host = f"[{address}]" if ":" in address else address
    base = f"http://{host}:{panel.config.port}"
    try:
        return PanelAuth(verify=f"{base}{VERIFY_PATH}", callback=f"{base}{CALLBACK_PATH}", login=f"https://{domain}/login")
    except ValidationError:
        return None
