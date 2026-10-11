"""Webhooki Gita (GitHub / Gitea / Forgejo): push na właściwą gałąź → redeploy."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..i18n import t
from ..services import deploy

router = APIRouter()


def verify_signature(secret: str, body: bytes, headers) -> bool:
    signature = headers.get("x-hub-signature-256") or ""
    if not signature and headers.get("x-gitea-signature"):
        signature = "sha256=" + headers["x-gitea-signature"]
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


@router.post("/hooks/git/{app_id}")
async def git_hook(app_id: int, request: Request) -> JSONResponse:
    panel = request.app.state.panel
    app = panel.db.get("apps", app_id)
    if not app or app["kind"] != "git":
        raise HTTPException(404, t("msg.no_git_app"))
    body = await request.body()
    secret = panel.sealer.unseal(app.get("webhook_secret"))
    if not secret or not verify_signature(secret, body, request.headers):
        raise HTTPException(403, t("msg.bad_signature"))
    event = request.headers.get("x-github-event") or request.headers.get("x-gitea-event") or "push"
    if event == "ping":
        return JSONResponse({"ok": True, "pong": True})
    if event != "push":
        return JSONResponse({"ok": True, "ignored": f"zdarzenie {event}"})
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, t("msg.bad_json")) from None
    spec, _ = deploy.load(panel, app)
    if payload.get("ref") != f"refs/heads/{spec.source.git_branch}":
        return JSONResponse({"ok": True, "ignored": t("msg.ignored_branch", ref=payload.get("ref"))})
    from .apps import start_deploy

    job = start_deploy(panel, app)
    commit = (payload.get("head_commit") or {}).get("id", "")[:12]
    panel.notify(t("msg.webhook_deploy", commit=commit, slug=app["slug"]))
    return JSONResponse({"ok": True, "job": job.id}, status_code=202)
