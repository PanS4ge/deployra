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
from ..i18n import t


def load(panel: Any, app: dict) -> tuple[AppSpec, dict]:
    blob = json.loads(panel.sealer.unseal(app["spec"]))
    return AppSpec.model_validate(blob["spec"]), blob.get("options", {})


def store(panel: Any, spec: AppSpec, options: dict) -> str:
    return panel.sealer.seal(json.dumps({"spec": spec.model_dump(mode="json"), "options": options}, ensure_ascii=False))


def create_app_row(panel: Any, name: str, node_id: int, spec: AppSpec, options: dict) -> int:
    if panel.db.get("apps", spec.slug, key="slug"):
        raise ValueError(t("msg.app_exists", slug=spec.slug))
    app_id = panel.db.insert(
        "apps", slug=spec.slug, name=name or spec.slug, node_id=node_id, kind=spec.source.kind, spec=store(panel, spec, options),
        domains=spec.domains, status="new", webhook_secret=panel.sealer.seal(secrets.token_urlsafe(24)), created_at=now(),
    )  # fmt: skip
    from . import uptime

    uptime.ensure_for(panel, "app", app_id, name or spec.slug, node_id)
    return app_id


def _upsert_site(panel: Any, app: dict, node: dict, host_port: int, options: dict, upstream_host: str | None = None) -> dict:
    if upstream_host:  # aplikacja w CT na Proxmoxie — bramka łączy się z IP kontenera LXC
        target = {"type": "address", "host": upstream_host, "port": host_port, "scheme": "http", "path": "", "via_node": node["id"]}
    else:
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
        raise JobFailed(t("msg.gateway_rejected"))


async def run(panel: Any, job: Job, app_id: int) -> dict:
    app = panel.db.get("apps", app_id)
    if not app:
        raise JobFailed(t("msg.no_app"))
    spec, options = load(panel, app)
    node = panel.node(app["node_id"])
    if not node:
        raise JobFailed(t("msg.app_node_missing"))
    first = app["status"] == "new"
    panel.db.update("apps", app_id, status="deploying", last_job=job.id, last_error=None)
    try:
        client = panel.node_client(node)
        job.log(t("deploy.start", slug=spec.slug, kind=spec.source.kind, node=node["name"]))
        if first:  # ochrona przed przepełnieniem — ponowne wdrożenie już nie pyta (świadoma decyzja)
            from . import resources

            problem = await resources.capacity_problem(panel, node, spec.memory_mb)
            if problem:
                raise JobFailed(problem + " " + t("res.retry_hint"))
        remote = await relay.start_and_follow(client, "/api/apps/deploy", spec.model_dump(mode="json"), job, "  ")
        result = remote.get("result") or {}
        host_port, upstream_host = result.get("host_port"), result.get("upstream_host")
        panel.db.update("apps", app_id, host_port=host_port)
        app = panel.db.get("apps", app_id)
        if upstream_host and node["transport"] == "tunnel" and not _routed(upstream_host, node.get("lan_subnets") or []):
            job.log(t("deploy.lxc_not_routed", ip=upstream_host))

        if app["domains"] and host_port:
            job.log(t("deploy.exposing", domains=", ".join(app["domains"])))
            site = _upsert_site(panel, app, node, host_port, options, upstream_host)
            await _push(panel, job)

            if options.get("dns"):
                try:
                    for line in await dns.ensure_records(panel, site):
                        job.log(f"  DNS: {line}")
                except (dns.DnsError, httpx.HTTPError) as exc:
                    job.log(t("deploy.dns_failed", error=exc))

            if options.get("https"):
                cert = certs.find_covering(panel, app["domains"])
                if cert:
                    job.log(t("deploy.cert_reuse", id=cert["id"], domains=", ".join(cert["domains"])))
                else:
                    cert_id = certs.create(panel, app["domains"])
                    await certs.issue(panel, job, cert_id)
                    cert = panel.db.get("certificates", cert_id)
                panel.db.update("sites", site["id"], cert_id=cert["id"], ssl_forced=int(bool(options.get("force_ssl", True))))
                panel.db.update("apps", app_id, cert_id=cert["id"])
                await _push(panel, job)

            await _check(panel, job, node, host_port, app["domains"][0], upstream_host)
        panel.db.update("apps", app_id, status="running", last_deploy_at=now())
        job.log(t("deploy.done"))
        panel.plugins.emit("app.deployed", app=panel.db.get("apps", app_id), job=job.id)
        return {"host_port": host_port}
    except (JobFailed, RemoteError, httpx.HTTPError, LookupError) as exc:
        message = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        panel.db.update("apps", app_id, status="error", last_error=message)
        panel.plugins.emit("app.failed", app=panel.db.get("apps", app_id), job=job.id, error=message)
        from . import alerts
        from .notify import Message

        await alerts.event(panel, "deploy_failed", Message(t("alerts.title.deploy_failed", name=app["name"]), message[:1500], "err"))
        raise JobFailed(message) from exc


def _routed(ip: str, subnets: list[str]) -> bool:
    import ipaddress

    try:
        address = ipaddress.ip_address(ip)
        return any(address in ipaddress.ip_network(s, strict=False) for s in subnets)
    except ValueError:
        return False


async def _check(panel: Any, job: Job, node: dict, port: int, domain: str, host: str | None = None) -> None:
    host = host or panel.node_upstream_host(node)
    client = panel.gateway_client()
    if not client or not host:
        return
    try:
        result = await client.post("/api/check", json={"url": f"http://{host}:{port}/", "host": domain})
    except (RemoteError, httpx.HTTPError) as exc:
        job.log(t("deploy.check_failed", error=exc))
        return
    if result.get("status"):
        job.log(t("deploy.check_ok", status=result["status"], ms=result.get("ms")))
    else:
        job.log(t("deploy.check_down", error=result.get("error")))


async def remove(panel: Any, job: Job, app_id: int, delete_data: bool) -> None:
    app = panel.db.get("apps", app_id)
    if not app:
        raise JobFailed(t("msg.no_app"))
    node = panel.node(app["node_id"])
    if node and node["status"] == "paired":
        try:
            await relay.start_and_follow(
                panel.node_client(node), f"/api/apps/{app['slug']}/remove", {"delete_data": delete_data}, job, "  "
            )
        except (RemoteError, httpx.HTTPError) as exc:
            job.log(t("deploy.node_error_removing", error=exc))
    if app.get("site_id"):
        await dns.remove_records(panel, app["site_id"])
        panel.db.delete("sites", app["site_id"])
        await _push(panel, job)
    panel.db.delete("apps", app_id)
    job.log(t("deploy.removed", slug=app["slug"]))
    panel.plugins.emit("app.removed", app=app)
