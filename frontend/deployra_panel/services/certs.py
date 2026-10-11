"""Certyfikaty: rekord w panelu, wystawienie na bramce (lego), synchronizacja dat ważności."""

from __future__ import annotations

from typing import Any

from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import CertRequest

from ..db import now
from . import relay
from ..i18n import t


def create(panel: Any, domains: list[str], challenge: str | None = None, name: str | None = None) -> int:
    if challenge is None:
        challenge = "dns-cloudflare" if any(d.startswith("*.") for d in domains) else "http"
    return panel.db.insert(
        "certificates", name=name or domains[0], domains=domains, challenge=challenge, status="new", created_at=now()
    )


def find_covering(panel: Any, domains: list[str]) -> dict | None:
    """Istniejący certyfikat obejmujący wszystkie domeny (także przez wildcard)."""

    def covers(cert_domains: list[str], domain: str) -> bool:
        for name in cert_domains:
            if name == domain:
                return True
            if name.startswith("*.") and domain.endswith(name[1:]) and domain.count(".") == name.count("."):
                return True
        return False

    for cert in panel.db.fetchall("SELECT * FROM certificates WHERE status IN ('valid', 'expiring') ORDER BY id"):
        if all(covers(cert["domains"], d) for d in domains):
            return cert
    return None


def request_for(panel: Any, cert: dict) -> CertRequest:
    email = panel.db.setting("acme_email") or ""
    token = panel.sealer.unseal(panel.db.setting("cloudflare_token")) if cert["challenge"] == "dns-cloudflare" else None
    return CertRequest(
        id=cert["id"], domains=cert["domains"], challenge=cert["challenge"], email=email, cloudflare_token=token,
        staging=bool(panel.db.setting("acme_staging", False)),
    )  # fmt: skip


async def issue(panel: Any, job: Job, cert_id: int) -> dict:
    cert = panel.db.get("certificates", cert_id)
    if not cert:
        raise JobFailed(t("msg.no_cert"))
    if cert["challenge"] == "custom":
        raise JobFailed(t("certs.msg.custom_no_reissue"))
    client = panel.gateway_client()
    if not client:
        raise JobFailed(t("msg.no_gateway_certs"))
    try:
        request = request_for(panel, cert)
    except ValueError as exc:
        raise JobFailed(t("certs.msg.issue_failed", error=exc)) from exc
    panel.db.update("certificates", cert_id, status="issuing", error=None, last_job=job.id)
    job.log(t("certs.msg.issuing", domains=", ".join(cert["domains"]), challenge=cert["challenge"]))
    try:
        snapshot = await relay.start_and_follow(client, "/api/certs/issue", request.model_dump(mode="json"), job, "  ")
    except JobFailed as exc:
        panel.db.update("certificates", cert_id, status="error", error=str(exc))
        raise
    info = snapshot.get("result") or {}
    panel.db.update("certificates", cert_id, status="valid", expires_at=info.get("not_after"), error=None)
    return info


async def sync_from_gateway(panel: Any) -> None:
    """Aktualizuje daty ważności na podstawie plików na bramce."""
    client = panel.gateway_client()
    if not client:
        return
    live = {c["id"]: c for c in await client.get("/api/certs")}
    for cert in panel.db.all("certificates"):
        info = live.get(cert["id"])
        if not info:
            if cert["status"] in ("valid", "expiring"):
                panel.db.update("certificates", cert["id"], status="missing")
            continue
        status = "expired" if info["days_left"] < 0 else ("expiring" if info["days_left"] < 14 else "valid")
        panel.db.update("certificates", cert["id"], status=status, expires_at=info["not_after"])
