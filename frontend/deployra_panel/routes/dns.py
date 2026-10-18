"""DNS: rekordy stref z Cloudflare — podgląd, edycja i z czym każdy jest połączony."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, Request

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import dns, dns_records
from ..web import render, toast

router = APIRouter(dependencies=[Depends(current_user)])
CHANGED = {"dns-changed": True}  # odświeża tabelę po zmianie


async def _cf_call(panel: Panel, work: Any) -> tuple[Any, str | None]:
    cf = dns.client(panel)
    if not cf:
        return None, t("dnsrec.no_token")
    try:
        return await asyncio.wait_for(work(cf), 30), None
    except (dns.DnsError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        return None, str(exc) or t("dnsrec.cf_down")
    finally:
        await cf.aclose()


@router.get("/dns")
async def dns_page(request: Request, zone: str = "", panel: Panel = Depends(get_panel)):
    zones, error = await _cf_call(panel, lambda cf: cf.zones())
    zones = sorted(zones or [], key=lambda z: z["name"])
    current = next((z for z in zones if z["id"] == zone), zones[0] if zones else None)
    return render(request, "dns/records.html", title=t("dnsrec.title"), zones=zones, zone=current, error=error,
                  has_token=bool(panel.db.setting("cloudflare_token")), types=dns_records.TYPES, ttls=dns_records.TTLS,
                  templates=dns_records.TEMPLATES, caa_tags=dns_records.CAA_TAGS,
                  ttl_text=dns_records.ttl_text, gateway_ip=await dns_records.gateway_ip(panel) if zones else None)  # fmt: skip


@router.get("/dns/records")
async def records_table(request: Request, zone: str, name: str = "", panel: Panel = Depends(get_panel)):
    records, error = await _cf_call(panel, lambda cf: cf.records(zone))
    link = dns_records.linker(panel)
    rows = [{**r, "link": link(zone, r)} for r in sorted(records or [], key=lambda r: (r["name"].count("."), r["name"], r["type"]))]
    return render(request, "dns/_table.html", rows=rows, error=error, zone_id=zone, zone_name=name, known=await dns_records.known_ips(panel),
                  ttls=dns_records.TTLS, proxiable=dns_records.PROXIABLE, ttl_text=dns_records.ttl_text)  # fmt: skip


@router.post("/dns/records")
async def create_record(request: Request, zone: str = Form(...), zone_name: str = Form(...), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    form = await request.form()
    try:
        data = dns_records.payload(form, zone_name)
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    record, error = await _cf_call(panel, lambda cf: cf.create_record(zone, data))
    if error:
        return toast(error, "err", status=400)
    panel.notify(t("dnsrec.notify_created", user=user["email"], type=data["type"], name=data["name"], content=dns_records.summary(data)))
    return toast(t("dnsrec.created", name=data["name"]), extra=CHANGED)


@router.post("/dns/templates")
async def apply_template(request: Request, zone: str = Form(...), zone_name: str = Form(...), template: str = Form(...),
                         panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    """Szablon (np. Minecraft, poczta) → kilka rekordów naraz; najpierw walidacja wszystkich, potem zapis."""
    form = await request.form()
    params = {k: str(form.get(k) or "") for k in ("name", "ip", "port", "proxied")}
    try:
        records = [dns_records.payload(r, zone_name) for r in dns_records.template_records(template, params, zone_name)]
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    done, failed = [], []
    for data in records:
        _, error = await _cf_call(panel, lambda cf, data=data: cf.create_record(zone, data))
        (failed if error else done).append(f"{data['type']} {data['name']}" + (f": {error}" if error else ""))
    if done:
        panel.notify(t("dnsrec.notify_template", user=user["email"], template=t("dnsrec.tpl." + template), records=", ".join(done)))
    if failed:
        return toast(t("dnsrec.template_partial", done=len(done), failed="; ".join(failed)[:300]), "warn" if done else "err",
                     status=200 if done else 400, extra=CHANGED)  # fmt: skip
    return toast(t("dnsrec.template_done", count=len(done)), extra=CHANGED)


@router.post("/dns/records/{record_id}")
async def update_record(request: Request, record_id: str, zone: str = Form(...), zone_name: str = Form(...), type: str = Form(...),
                        name: str = Form(...), panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    form = await request.form()
    try:
        data = dns_records.payload(form, zone_name, type)
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    _, error = await _cf_call(panel, lambda cf: cf.update_record(zone, record_id, data))
    if error:
        return toast(error, "err", status=400)
    panel.notify(t("dnsrec.notify_updated", user=user["email"], type=type, name=name, content=dns_records.summary(data)))
    return toast(t("dnsrec.updated", name=name), extra=CHANGED)


@router.post("/dns/records/{record_id}/delete")
async def delete_record(request: Request, record_id: str, zone: str = Form(...), name: str = Form(""), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    _, error = await _cf_call(panel, lambda cf: cf.delete(zone, record_id))
    if error:
        return toast(error, "err", status=400)
    for row in panel.db.fetchall("SELECT id FROM dns_records WHERE zone_id = ? AND record_id = ?", [zone, record_id]):
        panel.db.delete("dns_records", row["id"])  # rekord zakładany przez Deployrę — już go nie ma
    panel.notify(t("dnsrec.notify_deleted", user=user["email"], name=name))
    return toast(t("dnsrec.deleted", name=name), "warn", extra=CHANGED)
