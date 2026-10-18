"""Logowanie, wylogowanie i pierwsza konfiguracja."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from deployra_common.passwords import hash_password, verify_password

from ..context import Panel, get_panel
from ..db import now
from ..i18n import LANG_COOKIE, catalog, lang, t
from ..permissions import Permission
from ..security import SESSION_COOKIE
from ..services import forward_auth
from ..web import redirect, render, toast

router = APIRouter()
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def start_session(request: Request, response: Response, panel: Panel, user_id: int) -> None:
    sid, _ = panel.sessions.create(user_id, _client_ip(request), request.headers.get("user-agent"))
    secure = panel.config.secure_cookies or request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=panel.config.session_hours * 3600, httponly=True, samesite="lax", secure=secure, path="/"
    )


@router.get("/login")
def login_page(request: Request, next: str = "/", panel: Panel = Depends(get_panel)):
    if request.state.user:
        return redirect(request, next if next.startswith(forward_auth.START_PATH) else "/")
    return render(request, "auth/login.html", title=t("auth.title_login"), next=next if next.startswith("/") and not next.startswith("//") else "/")


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    panel: Panel = Depends(get_panel),
):
    ip = _client_ip(request)
    if panel.limiter.blocked(ip):
        return toast(t("msg.too_many_attempts"), "err", status=429)
    user = await run_in_threadpool(panel.db.fetchone, "SELECT * FROM users WHERE email = ?", [email.strip().lower()])
    ok = bool(user) and await run_in_threadpool(verify_password, password, user["password_hash"] if user else None)
    if not ok:
        panel.limiter.fail(ip)
        return toast(t("msg.bad_login"), "err", status=401)
    panel.limiter.reset(ip)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = redirect(request, target)
    start_session(request, response, panel, user["id"])
    return response


# --- logowanie Deployra przed aplikacją (forward auth, patrz services/forward_auth.py) -----------


@router.api_route("/auth/forward", methods=["GET", "HEAD"])
def forward_verify(request: Request, panel: Panel = Depends(get_panel)):
    """Bramka (auth_request): 204 z X-Deployra-User, gdy ciasteczko jest ważne i konto ma dostęp; inaczej 401."""
    host = forward_auth.hostname(request.headers.get("x-deployra-host"))
    user = forward_auth.authorize(panel, host, forward_auth.cookie_user(panel, request.cookies.get(forward_auth.FORWARD_COOKIE), host))
    headers = {"Cache-Control": "no-store"}
    if not user:
        return Response(status_code=401, headers=headers)
    return Response(status_code=204, headers={**headers, "X-Deployra-User": user["email"]})


@router.get("/auth/forward/start")
def forward_start(request: Request, rd: str = "", panel: Panel = Depends(get_panel)):
    """Zalogowany użytkownik → jednorazowy token i powrót na host aplikacji (przez callback bramki)."""
    user = request.state.user
    if not user or request.state.is_token_auth:
        return redirect(request, "/login")
    target = forward_auth.start(panel, user["id"], rd)
    if not target:
        return redirect(request, "/", t("auth.forward_failed"), "err")
    return RedirectResponse(target, status_code=302)


@router.get("/auth/forward/callback")
def forward_callback(request: Request, token: str = "", panel: Panel = Depends(get_panel)):
    """Host aplikacji: wymienia token na ciasteczko dla tego hosta (bez ciasteczka panelu) i wraca do rd."""
    host = forward_auth.hostname(request.headers.get("host"))
    taken = forward_auth.consume_token(panel, token, host)
    user = forward_auth.authorize(panel, host, taken[0]) if taken else None
    if not taken or not user:
        return PlainTextResponse(t("auth.forward_expired"), status_code=401, headers={"Cache-Control": "no-store"})
    response = RedirectResponse(f"https://{host}{taken[1]}", status_code=302)
    response.set_cookie(
        forward_auth.FORWARD_COOKIE, forward_auth.make_cookie(panel, user["id"], host), max_age=panel.config.session_hours * 3600,
        httponly=True, secure=True, samesite="lax", path="/",
    )  # fmt: skip
    response.headers["Cache-Control"] = "no-store"
    return response


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
    panel: Panel = Depends(get_panel),
):
    if panel.has_users():
        return redirect(request, "/")
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
    panel.notify(t("msg.welcome"), "ok")
    response = redirect(request, "/", t("msg.account_created"))
    start_session(request, response, panel, user_id)
    return response
