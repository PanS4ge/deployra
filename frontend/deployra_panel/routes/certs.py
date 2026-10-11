"""Certyfikaty: Let's Encrypt (HTTP-01 / Cloudflare DNS-01) i własne PEM."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request

from deployra_common.client import RemoteError
from deployra_common.schemas import CertUpload, check_domain

from ..context import Panel, get_panel
from ..db import now
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import certs as certs_svc
from ..services import proxy_state
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/certs")
async def certs_page(request: Request, panel: Panel = Depends(get_panel)):
    sync_error = None
    try:
        await certs_svc.sync_from_gateway(panel)
    except (RemoteError, httpx.HTTPError) as exc:
        sync_error = getattr(exc, "detail", None) or t("msg.gateway_not_responding")
    rows = panel.db.all("certificates")
    used = {}
    for site in panel.db.all("sites"):
        if site.get("cert_id"):
            used.setdefault(site["cert_id"], []).append(site["domains"][0])
    settings = panel.db.settings()
    return render(request, "certs.html", title=t("nav.certs"), certs=rows, used=used, sync_error=sync_error,
                  has_cf=bool(settings.get("cloudflare_token")), acme_email=settings.get("acme_email"),
                  staging=settings.get("acme_staging"))  # fmt: skip


@router.post("/certs")
async def create_cert(request: Request, domains: str = Form(...), challenge: str = Form("http"), panel: Panel = Depends(get_panel),
                user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    try:
        names = [check_domain(d) for d in domains.replace(",", "\n").splitlines() if d.strip()]
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    if not names:
        return toast(t("msg.need_domain"), "err", status=400)
    if challenge not in ("http", "dns-cloudflare"):
        challenge = "http"
    cert_id = certs_svc.create(panel, names, challenge)
    job = panel.jobs.start("cert", t("jobs.title_cert", name=", ".join(names)), lambda job: certs_svc.issue(panel, job, cert_id))
    return redirect(request, f"/jobs/{job.id}")


@router.post("/certs/{cert_id}/issue")
async def reissue(request: Request, cert_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    if not panel.db.get("certificates", cert_id):
        raise HTTPException(404, t("msg.no_cert"))
    job = panel.jobs.start("cert", t("jobs.title_cert", name=f"#{cert_id}"), lambda job: certs_svc.issue(panel, job, cert_id))
    return redirect(request, f"/jobs/{job.id}")


@router.post("/certs/upload")
async def upload(request: Request, name: str = Form(""), fullchain: str = Form(...), privkey: str = Form(...),
                 panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    client = panel.gateway_client()
    if not client:
        raise LookupError(t("msg.no_gateway_certs"))
    cert_id = panel.db.insert("certificates", name=name.strip() or t("certs.custom"), domains=[], challenge="custom", status="new", created_at=now())
    try:
        result = await client.post("/api/certs/upload", json=CertUpload(id=cert_id, fullchain=fullchain, privkey=privkey).model_dump())
    except (RemoteError, ValueError) as exc:
        panel.db.delete("certificates", cert_id)
        return toast(t("msg.cert_rejected", error=getattr(exc, "detail", exc)), "err", status=400)
    info = result.get("cert") or {}
    panel.db.update("certificates", cert_id, domains=info.get("domains") or [], status="valid", expires_at=info.get("not_after"),
                    name=name.strip() or (info.get("domains") or [t("certs.custom")])[0])  # fmt: skip
    return redirect(request, "/certs", t("msg.cert_uploaded"))


@router.post("/certs/{cert_id}/delete")
async def delete(request: Request, cert_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):
    panel.db.execute("UPDATE sites SET cert_id = NULL, ssl_forced = 0 WHERE cert_id = ?", [cert_id])
    panel.db.execute("UPDATE apps SET cert_id = NULL WHERE cert_id = ?", [cert_id])
    client = panel.gateway_client()
    if client:
        try:
            await client.delete(f"/api/certs/{cert_id}")
        except RemoteError:
            pass
    panel.db.delete("certificates", cert_id)
    await proxy_state.push(panel)
    return redirect(request, "/certs", t("msg.cert_deleted"))
