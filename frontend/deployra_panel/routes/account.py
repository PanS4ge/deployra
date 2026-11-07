"""Moje konto: zmiana hasła, 2FA (TOTP + kody zapasowe), aktywne sesje. Także dla kont „tylko przypisane serwery”."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool

from deployra_common import totp
from deployra_common.passwords import hash_password, verify_password

from ..context import Panel, get_panel
from ..deps import current_user
from ..i18n import t
from ..services import account_security
from ..web import redirect, render, toast

router = APIRouter()
_PENDING: dict[str, tuple[str, float]] = {}  # sesja → (nowy sekret 2FA czekający na potwierdzenie kodem, wygaśnięcie)


def _ip(request: Request) -> str:
    from .auth import _client_ip

    return _client_ip(request)


@router.get("/account")
def account_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    return render(request, "account.html", title=t("account.title"), sessions=panel.sessions.of_user(user["id"]),
                  current=account_security_handle(user), has_2fa=account_security.has_2fa(user),
                  backup_left=sum(1 for c in user.get("backup_codes") or [] if c), forced=bool(panel.db.setting("require_2fa")))  # fmt: skip


def account_security_handle(user: dict) -> str:
    from ..security import handle

    return handle(user.get("sid") or "")


async def _check_password(panel: Panel, request: Request, user: dict, password: str) -> bool:
    ip = _ip(request)
    if await run_in_threadpool(panel.limiter.blocked, ip):
        return False
    ok = await run_in_threadpool(verify_password, password, user.get("password_hash"))
    if not ok:
        await run_in_threadpool(panel.limiter.fail, ip, user["email"])
    return ok


@router.post("/account/password")
async def change_password(request: Request, current: str = Form(""), password: str = Form(""), password_repeat: str = Form(""),
                          panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):  # fmt: skip
    if user.get("token_id"):
        return toast(t("msg.forbidden"), "err", status=403)
    if not await _check_password(panel, request, user, current):
        return toast(t("msg.bad_current_password"), "err", status=403)
    if len(password) < 8:
        return toast(t("msg.password_short"), "err", status=400)
    if password != password_repeat:
        return toast(t("msg.passwords_differ"), "err", status=400)
    panel.db.update("users", user["id"], password_hash=await run_in_threadpool(hash_password, password))
    panel.sessions.destroy_others(user["id"], user.get("sid"))
    return redirect(request, "/account", t("account.password_changed"))


@router.post("/account/2fa/start")
def start_2fa(request: Request, user: dict = Depends(current_user)):
    if account_security.has_2fa(user):
        return toast(t("account.2fa_already"), "warn", status=409)
    secret = totp.new_secret()
    _PENDING[user["sid"]] = (secret, time.time() + 600)
    response = render(request, "account/_2fa_setup.html", secret=secret, uri=totp.uri(secret, user["email"]))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/account/2fa/enable")
def enable_2fa(request: Request, code: str = Form(""), panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    pending = _PENDING.get(user["sid"])
    if not pending or pending[1] < time.time():
        return toast(t("account.2fa_expired"), "err", status=400)
    step = totp.verify(pending[0], code)
    if step is None:
        return toast(t("account.bad_code"), "err", status=400)
    _PENDING.pop(user["sid"], None)
    panel.db.update("users", user["id"], totp_secret=panel.sealer.seal(pending[0]), totp_step=step)
    codes = account_security.new_backup_codes(panel, user["id"])
    panel.sessions.destroy_others(user["id"], user.get("sid"))
    panel.sessions.mark_reauth(user["sid"])
    response = render(request, "account/_backup_codes.html", codes=codes, fresh=True)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/account/2fa/backup")
def regenerate_backup(request: Request, code: str = Form(""), panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    if not account_security.has_2fa(user) or not account_security.check_code(panel, user, code):
        return toast(t("account.bad_code"), "err", status=403)
    codes = account_security.new_backup_codes(panel, user["id"])
    response = render(request, "account/_backup_codes.html", codes=codes, fresh=False)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/account/2fa/disable")
async def disable_2fa(request: Request, password: str = Form(""), code: str = Form(""), panel: Panel = Depends(get_panel),
                      user: dict = Depends(current_user)):  # fmt: skip
    if panel.db.setting("require_2fa"):
        return toast(t("account.2fa_required_by_admin"), "err", status=403)
    if not await _check_password(panel, request, user, password) or not account_security.check_code(panel, user, code):
        return toast(t("account.bad_password_or_code"), "err", status=403)
    panel.db.update("users", user["id"], totp_secret=None, totp_step=None, backup_codes=None)
    return redirect(request, "/account", t("account.2fa_disabled"))


@router.post("/account/sessions/{value}/revoke")
def revoke_session(request: Request, value: str, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    if not panel.sessions.destroy_by_handle(user["id"], value):
        return toast(t("account.no_session"), "err", status=404)
    return redirect(request, "/account", t("account.session_revoked"))


@router.post("/account/sessions/revoke-others")
def revoke_others(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    panel.sessions.destroy_others(user["id"], user.get("sid"))
    return redirect(request, "/account", t("account.others_revoked"))
