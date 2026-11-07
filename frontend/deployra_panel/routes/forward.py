"""Logowanie Deployra na hostach aplikacji (lista dostępu w trybie „Deployra”).

Bramka przekazuje do panelu przez tunel:
- `/__deployra_auth` (auth_request) → `GET /auth/forward`: 204 + X-Deployra-User albo 401,
- `/__deployra_auth/…` → `/auth/forward/…`: strona logowania kontami listy (i kodem 2FA), wylogowanie oraz callback
  logowania kontem panelu (`start` na domenie panelu → jednorazowy token → sesja na hoście aplikacji).

Strona logowania, TOTP i wylogowanie wymagają tokenu bramki (`X-Deployra-Gw`): bez niego panel odpowiada 403, więc
hasła da się zgadywać tylko przez bramkę — z jej limitami i banami. Dopiero z tokenem panel ufa `X-Real-IP`.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
import time
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from loguru import logger

from ..context import Panel, get_panel
from ..i18n import lang, t
from ..services import access_accounts, forward_auth
from ..web import redirect, safe_next, templates

router = APIRouter()

COOKIE_SECURE, COOKIE_PLAIN = "__Host-dpl_fs", "dpl_fs"  # __Host-: tylko HTTPS, bez Domain — ciasteczko jednego hosta
CSRF_COOKIE = "dpl_fs_csrf"
PRE_TTL = 300
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
PUBLIC = ("/auth/forward", "/auth/forward/callback", "/auth/forward/login", "/auth/forward/totp", "/auth/forward/logout")


def _host(request: Request) -> str:
    return forward_auth.hostname(request.headers.get("x-deployra-host") or request.headers.get("host"))


def _https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"


def _session_cookie(request: Request) -> str | None:
    return request.cookies.get(COOKIE_SECURE) or request.cookies.get(COOKIE_PLAIN)


def _gw(request: Request, panel: Panel) -> bool:
    return access_accounts.gw_ok(panel, request.headers.get("x-deployra-gw"))


def _ip(request: Request, panel: Panel) -> str:
    real = (request.headers.get("x-real-ip") or "").strip()
    if real and _gw(request, panel):
        return real[:64]
    return request.client.host if request.client else "?"


def _secure_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _set_session(response: Response, request: Request, raw: str, hours: int) -> None:
    secure = _https(request)
    response.set_cookie(COOKIE_SECURE if secure else COOKIE_PLAIN, raw, max_age=hours * 3600, httponly=True, secure=secure,
                        samesite="lax", path="/")  # fmt: skip


def _page(request: Request, panel: Panel, host: str, acl: dict, *, status: int = 200, step: str = "login", **ctx) -> Response:
    """Strona logowania z nowym tokenem CSRF (podwójne przesłanie: ciasteczko + pole formularza)."""
    csrf = secrets.token_urlsafe(24)
    rd = safe_next(ctx.pop("rd", "/"))
    panel_login = None
    domain = str(panel.db.setting("panel_domain") or "").strip()
    if acl.get("allow_panel", 1) and domain and step == "login":
        start = f"{forward_auth.START_PATH}?" + urlencode({"rd": f"https://{host}{rd}"})
        panel_login = f"https://{domain}/login?" + urlencode({"next": start})
    response = templates.TemplateResponse(
        request, "auth/forward_login.html",
        {"t": t, "lang": lang(), "host": host, "csrf": csrf, "rd": rd, "step": step, "panel_login": panel_login, **ctx},
        status_code=status,
    )  # fmt: skip
    response.set_cookie(CSRF_COOKIE, csrf, max_age=1800, httponly=True, secure=_https(request), samesite="strict", path="/__deployra_auth")
    return _secure_headers(response)


def _csrf_ok(request: Request, host: str, token: str) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    if not token or not cookie or not hmac.compare_digest(token.encode(), cookie.encode()):
        return False
    origin = request.headers.get("origin")
    if not origin:  # starsze przeglądarki nie wysyłają Origin przy zwykłym formularzu — wystarcza podwójne przesłanie tokenu
        return True
    return (urlsplit(origin).hostname or "").lower() == host  # „null” i obce hosty odpadają


def _context(request: Request, panel: Panel) -> tuple[str, dict] | Response:
    """Host i jego lista z logowaniem Deployra; bez tokenu bramki albo dla obcego hosta — odmowa."""
    if not _gw(request, panel):
        return PlainTextResponse(t("fwd.no_gateway"), status_code=403, headers={"Cache-Control": "no-store"})
    host = _host(request)
    acl = forward_auth.site_acl(panel, host)
    if not acl:
        return PlainTextResponse(t("fwd.no_login_here"), status_code=404, headers={"Cache-Control": "no-store"})
    return host, acl


async def _ban(panel: Panel, ip: str) -> None:
    """Zgadywanie haseł z jednego adresu: ban na bramce (nie dla adresów prywatnych i zaufanych pośredników)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return
    if address.is_private or address.is_loopback or ip in panel.trusted_proxies():
        return
    client = panel.gateway_client()
    if not client:
        return
    try:
        await client.post("/api/bans", json={"target": ip, "minutes": 60, "reason": t("fwd.ban_reason")})
        logger.warning(t("fwd.banned_log", ip=ip))
    except Exception as exc:  # noqa: BLE001 — limit w panelu i tak działa
        logger.warning(f"ban {ip}: {exc}")


# --- bramka: sprawdzenie sesji ------------------------------------------------------------------


@router.api_route("/auth/forward", methods=["GET", "HEAD"])
def forward_verify(request: Request, panel: Panel = Depends(get_panel)):
    """Bramka (auth_request): 204 z X-Deployra-User, gdy sesja (albo starsze podpisane ciasteczko) jest ważna; inaczej 401."""
    host = forward_auth.hostname(request.headers.get("x-deployra-host"))
    headers = {"Cache-Control": "no-store"}
    found = access_accounts.session(panel, _session_cookie(request), host)
    if found:
        return Response(status_code=204, headers={**headers, "X-Deployra-User": found["who"]})
    legacy = forward_auth.authorize(panel, host, forward_auth.cookie_user(panel, request.cookies.get(forward_auth.FORWARD_COOKIE), host))
    if legacy:
        return Response(status_code=204, headers={**headers, "X-Deployra-User": legacy["email"]})
    return Response(status_code=401, headers=headers)


# --- strona logowania kontami listy ----------------------------------------------------------------


@router.get("/auth/forward/login")
def login_page(request: Request, rd: str = "/", panel: Panel = Depends(get_panel)):
    ctx = _context(request, panel)
    if isinstance(ctx, Response):
        return ctx
    host, acl = ctx
    if access_accounts.session(panel, _session_cookie(request), host):  # już zalogowany — prosto do aplikacji
        return _secure_headers(RedirectResponse(safe_next(rd), status_code=303))
    return _page(request, panel, host, acl, rd=rd)


@router.post("/auth/forward/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form(""), rd: str = Form("/"),
                panel: Panel = Depends(get_panel)):  # fmt: skip
    ctx = _context(request, panel)
    if isinstance(ctx, Response):
        return ctx
    host, acl = ctx
    ip, username = _ip(request, panel), username.strip()[:64]
    if not _csrf_ok(request, host, csrf):
        return _page(request, panel, host, acl, status=400, rd=rd, username=username, error=t("fwd.expired_form"))
    limiter = panel.fwd_limiter
    if await run_in_threadpool(limiter.blocked, ip):
        await _ban(panel, ip)
        return _page(request, panel, host, acl, status=429, rd=rd, error=t("fwd.too_many"))
    key = f"{acl['id']}:{username.lower()}"
    wait = await run_in_threadpool(limiter.delay, key)
    if wait:
        await asyncio.sleep(wait)
    account, reason = await run_in_threadpool(access_accounts.authenticate, panel, acl["id"], username, password, ip)
    if not account:
        await run_in_threadpool(limiter.fail, ip, key)
        if reason == "locked_now":
            from ..services import alerts
            from ..services.notify import Message

            await alerts.event(panel, "access_account_locked",
                               Message(t("alerts.title.access_account_locked", name=username, host=host), t("fwd.locked_detail", ip=ip), "warn"))  # fmt: skip
        from ..services import audit

        audit.record(panel, "access_login", email=username, ip=ip, target=host, ok=False, reason=reason)
        error = t("fwd.locked") if reason.startswith("locked") else t("fwd.bad_login")
        return _page(request, panel, host, acl, status=401, rd=rd, username=username, error=error)
    if account.get("totp_secret"):
        pre = forward_auth._pack(panel, "pre", {"a": account["id"], "h": host, "e": time.time() + PRE_TTL, "n": secrets.token_urlsafe(12)})
        return _page(request, panel, host, acl, step="totp", rd=rd, pre=pre)
    if acl.get("require_totp"):
        return _page(request, panel, host, acl, status=403, rd=rd, error=t("fwd.totp_required"))
    return await _finish(request, panel, host, acl, account, ip, rd)


@router.post("/auth/forward/totp")
async def totp_step(request: Request, code: str = Form(""), pre: str = Form(""), csrf: str = Form(""), rd: str = Form("/"),
                    panel: Panel = Depends(get_panel)):  # fmt: skip
    ctx = _context(request, panel)
    if isinstance(ctx, Response):
        return ctx
    host, acl = ctx
    ip = _ip(request, panel)
    data = forward_auth._unpack(panel, "pre", pre)
    if not _csrf_ok(request, host, csrf) or not data or data.get("h") != host:
        return _page(request, panel, host, acl, status=400, rd=rd, error=t("fwd.expired_form"))
    account = panel.db.get("access_accounts", int(data.get("a") or 0))
    if not account or account["acl_id"] != acl["id"] or not account["enabled"]:
        return _page(request, panel, host, acl, status=401, rd=rd, error=t("fwd.bad_login"))
    limiter = panel.fwd_limiter
    key = f"{acl['id']}:{account['username'].lower()}"
    if await run_in_threadpool(limiter.blocked, ip):
        await _ban(panel, ip)
        return _page(request, panel, host, acl, status=429, rd=rd, error=t("fwd.too_many"))
    if not await run_in_threadpool(access_accounts.check_totp, panel, account, code):
        await run_in_threadpool(limiter.fail, ip, key)
        return _page(request, panel, host, acl, status=401, step="totp", rd=rd, pre=pre, error=t("fwd.bad_code"))
    return await _finish(request, panel, host, acl, account, ip, rd)


async def _finish(request: Request, panel: Panel, host: str, acl: dict, account: dict, ip: str, rd: str) -> Response:
    await run_in_threadpool(panel.fwd_limiter.reset, ip, f"{acl['id']}:{account['username'].lower()}")
    raw = await run_in_threadpool(access_accounts.create_session, panel, acl, host, ip, request.headers.get("user-agent"), account["id"])
    await run_in_threadpool(access_accounts.logged_in, panel, account["id"], ip)
    from ..services import audit

    audit.record(panel, "access_login", email=account["username"], ip=ip, target=host)
    response = RedirectResponse(safe_next(rd), status_code=303)
    _set_session(response, request, raw, int(acl.get("session_hours") or access_accounts.DEFAULT_SESSION_HOURS))
    response.delete_cookie(CSRF_COOKIE, path="/__deployra_auth")
    return _secure_headers(response)


@router.get("/auth/forward/logout")
def logout(request: Request, panel: Panel = Depends(get_panel)):
    ctx = _context(request, panel)
    if isinstance(ctx, Response):
        return ctx
    host, acl = ctx
    access_accounts.revoke(panel, _session_cookie(request))
    response = _page(request, panel, host, acl, notice=t("fwd.logged_out"))
    for name in (COOKIE_SECURE, COOKIE_PLAIN, forward_auth.FORWARD_COOKIE):
        response.delete_cookie(name, path="/", secure=name.startswith("__Host-"))
    return response


# --- logowanie kontem panelu (domena panelu → host aplikacji) ------------------------------------


@router.get("/auth/forward/start")
def forward_start(request: Request, rd: str = "", panel: Panel = Depends(get_panel)):
    """Zalogowany użytkownik panelu → jednorazowy token i powrót na host aplikacji (przez callback bramki)."""
    user = request.state.user
    if not user or request.state.is_token_auth:
        return redirect(request, "/login")
    target = forward_auth.start(panel, user["id"], rd)
    if not target:
        return redirect(request, "/", t("auth.forward_failed"), "err")
    return RedirectResponse(target, status_code=302)


@router.get("/auth/forward/callback")
def forward_callback(request: Request, token: str = "", panel: Panel = Depends(get_panel)):
    """Host aplikacji: wymienia token na sesję dla tego hosta (bez ciasteczka panelu) i wraca do rd."""
    host = forward_auth.hostname(request.headers.get("host"))
    taken = forward_auth.consume_token(panel, token, host)
    user = forward_auth.authorize(panel, host, taken[0]) if taken else None
    acl = forward_auth.site_acl(panel, host) if user else None
    if not taken or not user or not acl:
        return PlainTextResponse(t("auth.forward_expired"), status_code=401, headers={"Cache-Control": "no-store"})
    raw = access_accounts.create_session(panel, acl, host, _ip(request, panel), request.headers.get("user-agent"), user_id=user["id"])
    response = RedirectResponse(f"https://{host}{taken[1]}", status_code=302)
    secure = True  # callback zawsze przez https:// (adres buduje forward_auth.start)
    response.set_cookie(COOKIE_SECURE if secure else COOKIE_PLAIN, raw, max_age=int(acl.get("session_hours") or 12) * 3600,
                        httponly=True, secure=secure, samesite="lax", path="/")  # fmt: skip
    response.headers["Cache-Control"] = "no-store"
    return response
