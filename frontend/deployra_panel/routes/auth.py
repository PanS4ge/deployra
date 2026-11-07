"""Logowanie, wylogowanie i pierwsza konfiguracja."""

from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from deployra_common.passwords import dummy_hash, hash_password, needs_rehash, verify_password

from ..context import Panel, get_panel
from ..db import now
from ..i18n import LANG_COOKIE, catalog, lang, t
from ..permissions import Permission
from ..security import SESSION_COOKIE, client_ip
from ..services import account_security, audit, forward_auth
from ..web import redirect, render, safe_next, toast

router = APIRouter()
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str:
    panel: Panel = request.app.state.panel
    return client_ip(request, panel.trusted_proxies())


def start_session(request: Request, response: Response, panel: Panel, user_id: int) -> str:
    old = request.cookies.get(SESSION_COOKIE)
    if old:  # logowanie zawsze dostaje nową sesję; poprzednia z tej przeglądarki przestaje działać
        panel.sessions.destroy(old)
    sid, _ = panel.sessions.create(user_id, _client_ip(request), request.headers.get("user-agent"))
    secure = panel.config.secure_cookies or request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=panel.config.session_hours * 3600, httponly=True, samesite="lax", secure=secure, path="/"
    )
    return sid


@router.get("/login")
def login_page(request: Request, next: str = "/", panel: Panel = Depends(get_panel)):
    next = safe_next(next)
    if request.state.user:
        return redirect(request, next if next.startswith(forward_auth.START_PATH) else "/")
    return render(request, "auth/login.html", title=t("auth.title_login"), next=next)


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    panel: Panel = Depends(get_panel),
):
    ip, account = _client_ip(request), email.strip().lower()[:190]
    if await run_in_threadpool(panel.limiter.blocked, ip):
        return toast(t("msg.too_many_attempts"), "err", status=429)
    wait = await run_in_threadpool(panel.limiter.delay, account)
    if wait:  # zgadywanie hasła jednego konta zwalnia, ale go nie blokuje (obcy nie zablokuje właściciela)
        await asyncio.sleep(wait)
    user = await run_in_threadpool(panel.db.fetchone, "SELECT * FROM users WHERE email = ?", [account])
    # nieistniejące konto też liczy hash — czas odpowiedzi nie zdradza, które adresy mają konto
    ok = await run_in_threadpool(verify_password, password, user["password_hash"] if user else dummy_hash())
    if not ok or not user:
        await run_in_threadpool(panel.limiter.fail, ip, account)
        audit.record(panel, "login", email=account, ip=ip, ok=False, user=user if user else None)
        await _failed_spike(panel)
        return toast(t("msg.bad_login"), "err", status=401)
    await run_in_threadpool(panel.limiter.reset, ip, account)
    if needs_rehash(user["password_hash"]):  # starszy, słabszy hash — przeliczamy przy okazji
        panel.db.update("users", user["id"], password_hash=await run_in_threadpool(hash_password, password))
    if account_security.has_2fa(user):  # drugi krok: kod z aplikacji — sesja powstaje dopiero po nim
        from urllib.parse import urlencode

        response = redirect(request, "/login/2fa?" + urlencode({"next": safe_next(next)}))
        pending = forward_auth._pack(panel, "login-2fa", {"u": user["id"], "e": time.time() + PENDING_TTL})
        response.set_cookie(PENDING_COOKIE, pending, max_age=PENDING_TTL, httponly=True, samesite="strict",
                            secure=panel.config.secure_cookies or request.url.scheme == "https", path="/login")  # fmt: skip
        return response
    response = redirect(request, safe_next(next))
    start_session(request, response, panel, user["id"])
    await _logged_in(panel, user, ip, request)
    return response


async def _logged_in(panel: Panel, user: dict, ip: str, request: Request) -> None:
    """Wpis w dzienniku + alert, gdy konto loguje się z adresu, z którego jeszcze nigdy się nie logowało."""
    new_ip = audit.had_logins(panel, user["id"]) and not audit.known_ip(panel, user["id"], ip)
    audit.record(panel, "login", user=user, ip=ip, agent=(request.headers.get("user-agent") or "")[:200])
    if new_ip:
        from ..services import alerts
        from ..services.notify import Message

        await alerts.event(panel, "login_new_ip", Message(t("alerts.title.login_new_ip", name=user["email"]), t("account.new_ip_text", ip=ip,
                           agent=(request.headers.get("user-agent") or "?")[:200]), "warn"))  # fmt: skip


async def _failed_spike(panel: Panel) -> None:
    if audit.recent_failures(panel, 10) in (20, 100, 500):  # raz przy przekroczeniu progu, nie przy każdej próbie
        from ..services import alerts
        from ..services.notify import Message

        await alerts.event(panel, "login_failed_spike", Message(t("alerts.title.login_failed_spike", name="panel"),
                           t("account.failed_spike_text", count=audit.recent_failures(panel, 10)), "err"))  # fmt: skip


PENDING_COOKIE, PENDING_TTL = "dpl_login2fa", 300


def _pending_user(request: Request, panel: Panel) -> dict | None:
    data = forward_auth._unpack(panel, "login-2fa", request.cookies.get(PENDING_COOKIE))
    return panel.db.get("users", int(data["u"])) if data and isinstance(data.get("u"), int) else None


@router.get("/login/2fa")
def login_2fa_page(request: Request, next: str = "/", panel: Panel = Depends(get_panel)):
    if not _pending_user(request, panel):
        return redirect(request, "/login")
    return render(request, "auth/login_2fa.html", title=t("auth.title_login"), next=safe_next(next))


@router.post("/login/2fa")
async def login_2fa(request: Request, code: str = Form(""), next: str = Form("/"), panel: Panel = Depends(get_panel)):
    ip = _client_ip(request)
    user = _pending_user(request, panel)
    if not user:
        return toast(t("account.2fa_expired"), "err", status=401)
    if await run_in_threadpool(panel.limiter.blocked, ip):
        return toast(t("msg.too_many_attempts"), "err", status=429)
    if not await run_in_threadpool(account_security.check_code, panel, user, code):
        await run_in_threadpool(panel.limiter.fail, ip, user["email"])
        audit.record(panel, "login_2fa", user=user, ip=ip, ok=False)
        await _failed_spike(panel)
        return toast(t("account.bad_code"), "err", status=401)
    await run_in_threadpool(panel.limiter.reset, ip, user["email"])
    response = redirect(request, safe_next(next))
    sid = start_session(request, response, panel, user["id"])
    panel.sessions.mark_reauth(sid)  # świeżo po 2FA — WebSSH bez ponownego pytania przez chwilę
    response.delete_cookie(PENDING_COOKIE, path="/login")
    await _logged_in(panel, user, ip, request)
    return response


# logowanie Deployra na hostach aplikacji: routes/forward.py


@router.post("/logout")
def logout(request: Request, panel: Panel = Depends(get_panel)):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        panel.sessions.destroy(sid)
    response = redirect(request, "/login", t("msg.logged_out"))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/language")
def set_language(request: Request, lang: str = Form(...), panel: Panel = Depends(get_panel)):
    """Język interfejsu: zapisany przy koncie (gdy zalogowany) i w ciasteczku (dla ekranu logowania)."""
    code = catalog.supported(lang)
    if not code:
        return toast("?", "err", status=400)
    user = request.state.user
    if user:
        panel.db.update("users", user["id"], lang=code)
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    response.set_cookie(LANG_COOKIE, code, max_age=365 * 86400, samesite="lax", path="/")
    return response


@router.get("/setup")
def setup_page(request: Request, panel: Panel = Depends(get_panel)):
    if panel.has_users():
        return redirect(request, "/")
    panel.setup_code()
    host = (request.headers.get("host") or "").split(":")[0]
    parts = host.split(".")
    base = ".".join(parts[-2:]) if len(parts) >= 2 and not host.replace(".", "").isdigit() else "example.com"
    return render(request, "auth/setup.html", title=t("auth.title_setup"), host=host, base=base, db=panel.db.dialect)


@router.post("/setup")
async def setup(
    request: Request,
    name: str = Form(""),
    surname: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    password_repeat: str = Form(...),
    panel_domain: str = Form(""),
    base_domain: str = Form(""),
    acme_email: str = Form(""),
    cloudflare_token: str = Form(""),
    setup_code: str = Form(""),
    panel: Panel = Depends(get_panel),
):
    if panel.has_users():
        return redirect(request, "/")
    import hmac

    expected = panel.setup_code() or ""
    if not hmac.compare_digest(setup_code.strip().upper().encode(), expected.encode()):
        ip = _client_ip(request)
        await run_in_threadpool(panel.limiter.fail, ip)
        if await run_in_threadpool(panel.limiter.blocked, ip):
            return toast(t("msg.too_many_attempts"), "err", status=429)
        return toast(t("setup.bad_code"), "err", status=403)
    email = email.strip().lower()
    if not EMAIL.match(email):
        return toast(t("msg.bad_email"), "err", status=400)
    if len(password) < 8:
        return toast(t("msg.password_short"), "err", status=400)
    if password != password_repeat:
        return toast(t("msg.passwords_differ"), "err", status=400)
    base = base_domain.strip().lower().removeprefix("*.").strip(".")
    password_hash = await run_in_threadpool(hash_password, password)
    with panel.db.transaction():
        user_id = panel.db.insert(
            "users", email=email, password_hash=password_hash, name=name.strip()[:60], surname=surname.strip()[:60],
            permissions=Permission.FULL_ACCESS.value, undeletable=1, created_at=now(), lang=lang(),
        )  # fmt: skip
        panel.db.set_setting("panel_domain", panel_domain.strip().lower())
        panel.db.set_setting("base_domain", base)
        panel.db.set_setting("acme_email", (acme_email.strip() or email).lower())
        panel.db.set_setting("cloudflare_token", panel.sealer.seal(cloudflare_token.strip()))
        panel.db.set_setting("gravatar_default", "identicon")
    panel.drop_setup_code()
    panel.notify(t("msg.welcome"), "ok")
    response = redirect(request, "/", t("msg.account_created"))
    start_session(request, response, panel, user_id)
    return response
