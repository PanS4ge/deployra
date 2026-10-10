"""Pipeline aplikacji: węzeł → host proxy na bramce → DNS → certyfikat → test HTTP."""

from __future__ import annotations

import json
import secrets
from typing import Any

import httpx

from deployra_common.client import RemoteError
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import AppSpec

from ..db import now
from . import certs, dns, proxy_state, relay


def load(panel: Any, app: dict) -> tuple[AppSpec, dict]:
    blob = json.loads(panel.sealer.unseal(app["spec"]))
    return AppSpec.model_validate(blob["spec"]), blob.get("options", {})


def store(panel: Any, spec: AppSpec, options: dict) -> str:
    return panel.sealer.seal(json.dumps({"spec": spec.model_dump(mode="json"), "options": options}, ensure_ascii=False))


def create_app_row(panel: Any, name: str, node_id: int, spec: AppSpec, options: dict) -> int:
    if panel.db.get("apps", spec.slug, key="slug"):
        raise ValueError(f"aplikacja o nazwie {spec.slug} już istnieje")
    return panel.db.insert(
        "apps", slug=spec.slug, name=name or spec.slug, node_id=node_id, kind=spec.source.kind, spec=store(panel, spec, options),
        domains=spec.domains, status="new", webhook_secret=panel.sealer.seal(secrets.token_urlsafe(24)), created_at=now(),
    )  # fmt: skip


def _upsert_site(panel: Any, app: dict, node: dict, host_port: int, options: dict) -> dict:
    target = {"type": "node", "node_id": node["id"], "port": host_port, "scheme": "http", "path": ""}
    site = panel.db.get("sites", app["site_id"]) if app.get("site_id") else None
    values = {"domains": app["domains"], "target": target, "websockets": 1, "dns": int(bool(options.get("dns")))}
    if site:
        panel.db.update("sites", site["id"], **values)
        return panel.db.get("sites", site["id"])
    site_id = panel.db.insert("sites", kind="proxy", app_id=app["id"], source="app", created_at=now(), block_exploits=1, **values)
    panel.db.update("apps", app["id"], site_id=site_id)
    return panel.db.get("sites", site_id)


async def _push(panel: Any, job: Job) -> None:
    result = await proxy_state.push(panel)
    message, level = proxy_state.push_message(result)
    job.log(("✔ " if level == "ok" else "⚠ ") + message)
    if level == "err":
        raise JobFailed("bramka odrzuciła konfigurację proxy")


async def run(panel: Any, job: Job, app_id: int) -> dict:
    app = panel.db.get("apps", app_id)
    if not app:
        raise JobFailed("nie ma takiej aplikacji")
    spec, options = load(panel, app)
    node = panel.node(app["node_id"])
    if not node:
        raise JobFailed("węzeł aplikacji nie istnieje")
    panel.db.update("apps", app_id, status="deploying", last_job=job.id, last_error=None)
    try:
        client = panel.node_client(node)
        job.log(f"▶ Wdrażam {spec.slug} ({spec.source.kind}) na węźle {node['name']}")
        remote = await relay.start_and_follow(client, "/api/apps/deploy", spec.model_dump(mode="json"), job, "  ")
        host_port = (remote.get("result") or {}).get("host_port")
        panel.db.update("apps", app_id, host_port=host_port)
        app = panel.db.get("apps", app_id)

        if app["domains"] and host_port:
            job.log(f"🌐 Wystawiam {', '.join(app['domains'])} przez Deployra-WG")
            site = _upsert_site(panel, app, node, host_port, options)
            await _push(panel, job)

            if options.get("dns"):
                try:
                    for line in await dns.ensure_records(panel, site):
                        job.log(f"  DNS: {line}")
                except (dns.DnsError, httpx.HTTPError) as exc:
                    job.log(f"⚠ DNS: {exc} — ustaw rekord ręcznie")

            if options.get("https"):
                cert = certs.find_covering(panel, app["domains"])
                if cert:
                    job.log(f"🔐 Używam istniejącego certyfikatu #{cert['id']} ({', '.join(cert['domains'])})")
                else:
                    cert_id = certs.create(panel, app["domains"])
                    await certs.issue(panel, job, cert_id)
                    cert = panel.db.get("certificates", cert_id)
                panel.db.update("sites", site["id"], cert_id=cert["id"], ssl_forced=int(bool(options.get("force_ssl", True))))
                panel.db.update("apps", app_id, cert_id=cert["id"])
                await _push(panel, job)

            await _check(panel, job, node, host_port, app["domains"][0])
        panel.db.update("apps", app_id, status="running", last_deploy_at=now())
        job.log("✨ Gotowe!")
        return {"host_port": host_port}
    except (JobFailed, RemoteError, httpx.HTTPError, LookupError) as exc:
        message = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        panel.db.update("apps", app_id, status="error", last_error=message)
        raise JobFailed(message) from exc


async def _check(panel: Any, job: Job, node: dict, port: int, domain: str) -> None:
    host = panel.node_upstream_host(node)
    client = panel.gateway_client()
    if not client or not host:
        return
    try:
        result = await client.post("/api/check", json={"url": f"http://{host}:{port}/", "host": domain})
    except (RemoteError, httpx.HTTPError) as exc:
        job.log(f"⚠ Nie udało się sprawdzić aplikacji: {exc}")
        return
    if result.get("status"):
        job.log(f"🩺 Test przez tunel: HTTP {result['status']} w {result.get('ms')} ms")
    else:
        job.log(f"⚠ Aplikacja nie odpowiada przez tunel: {result.get('error')}")


async def remove(panel: Any, job: Job, app_id: int, delete_data: bool) -> None:
    app = panel.db.get("apps", app_id)
    if not app:
        raise JobFailed("nie ma takiej aplikacji")
    node = panel.node(app["node_id"])
    if node and node["status"] == "paired":
        try:
            await relay.start_and_follow(
                panel.node_client(node), f"/api/apps/{app['slug']}/remove", {"delete_data": delete_data}, job, "  "
            )
        except (RemoteError, httpx.HTTPError) as exc:
            job.log(f"⚠ Węzeł: {exc} — usuwam z panelu mimo to")
    if app.get("site_id"):
        await dns.remove_records(panel, app["site_id"])
        panel.db.delete("sites", app["site_id"])
        await _push(panel, job)
    panel.db.delete("apps", app_id)
    job.log(f"Usunięto aplikację {app['slug']}")
