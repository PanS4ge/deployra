"""Dziennik zdarzeń: kto, kiedy, skąd i co zmienił.

Dwa źródła wpisów:
- middleware — każde żądanie zmieniające stan (POST/PUT/PATCH/DELETE) zalogowanego konta albo tokenu: metoda, ścieżka,
  status, konto, IP. Treść formularza nie trafia do dziennika (tam bywają hasła i klucze);
- `record()` w kodzie — zdarzenia z nazwą: logowania (także nieudane), 2FA, WebSSH, logowania list dostępu.
"""

from __future__ import annotations

import csv
import io
import secrets
import time
from typing import Any

SKIP_PREFIXES = ("/auth/forward", "/hooks/", "/minecraft/bridge/", "/static/", "/language", "/notifications")
DEFAULT_DAYS = 90


def record(panel: Any, action: str, *, user: dict | None = None, email: str | None = None, ip: str | None = None, target: str = "",
           ok: bool = True, method: str | None = None, path: str | None = None, status: int | None = None, **details: Any) -> None:  # fmt: skip
    try:
        panel.db.insert(
            "audit_log", at=time.time(), user_id=(user or {}).get("id"), user_email=(email or (user or {}).get("email") or "")[:190],
            ip=(ip or "")[:64], action=action[:64], target=str(target)[:190], ok=int(bool(ok)), method=method, path=(path or "")[:300] or None,
            status=status, details={k: v for k, v in details.items() if v is not None} or None,
        )  # fmt: skip
        if secrets.randbelow(500) == 0:
            cleanup(panel)
    except Exception:  # noqa: BLE001 — dziennik nigdy nie psuje właściwej akcji
        pass


def from_request(panel: Any, request: Any, status: int) -> None:
    """Wpis z middleware dla żądania zmieniającego stan."""
    path = request.url.path
    if request.method in ("GET", "HEAD", "OPTIONS") or path.startswith(SKIP_PREFIXES):
        return
    user = getattr(request.state, "user", None)
    if not user:
        return
    from ..security import client_ip

    record(panel, "token" if getattr(request.state, "is_token_auth", False) else "request", user=user,
           ip=client_ip(request, panel.trusted_proxies()), ok=status < 400, method=request.method, path=path, status=status,
           token=user.get("token_name"))  # fmt: skip


def listing(panel: Any, *, email: str = "", action: str = "", query: str = "", days: int = 30, limit: int = 200, offset: int = 0) -> list[dict]:
    where, params = ["at > ?"], [time.time() - days * 86400]
    if email:
        where.append("user_email = ?")
        params.append(email)
    if action:
        where.append("action = ?")
        params.append(action)
    if query:
        where.append("(path LIKE ? OR target LIKE ? OR ip LIKE ?)")
        params += [f"%{query}%"] * 3
    sql = f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT {int(limit)} OFFSET {int(offset)}"
    return panel.db.fetchall(sql, params)


def actions(panel: Any) -> list[str]:
    return [r["action"] for r in panel.db.fetchall("SELECT DISTINCT action FROM audit_log ORDER BY action")]


def emails(panel: Any) -> list[str]:
    return [r["user_email"] for r in panel.db.fetchall("SELECT DISTINCT user_email FROM audit_log WHERE user_email <> '' ORDER BY user_email")]


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time", "user", "ip", "action", "target", "ok", "method", "path", "status", "details"])
    for r in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["at"]))
        writer.writerow([stamp, r["user_email"], r["ip"], r["action"], r["target"], r["ok"], r.get("method") or "", r.get("path") or "",
                         r.get("status") or "", r.get("details") or ""])  # fmt: skip
    return out.getvalue()


def cleanup(panel: Any) -> None:
    days = int(panel.db.setting("audit_days") or DEFAULT_DAYS)
    panel.db.execute("DELETE FROM audit_log WHERE at < ?", [time.time() - days * 86400])


def known_ip(panel: Any, user_id: int, ip: str) -> bool:
    """Czy konto logowało się już kiedyś z tego adresu (alert „logowanie z nowego adresu”)."""
    return bool(panel.db.fetchone("SELECT id FROM audit_log WHERE user_id = ? AND action = 'login' AND ok = 1 AND ip = ? LIMIT 1", [user_id, ip]))


def had_logins(panel: Any, user_id: int) -> bool:
    return bool(panel.db.fetchone("SELECT id FROM audit_log WHERE user_id = ? AND action = 'login' AND ok = 1 LIMIT 1", [user_id]))


def recent_failures(panel: Any, minutes: int = 10) -> int:
    row = panel.db.fetchone("SELECT COUNT(*) AS n FROM audit_log WHERE action IN ('login', 'login_2fa') AND ok = 0 AND at > ?",
                            [time.time() - minutes * 60])  # fmt: skip
    return int((row or {}).get("n") or 0)
