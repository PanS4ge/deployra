import asyncio

from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, uptime


class Node:
    def __init__(self, containers):
        self.containers, self.calls = containers, []

    async def get(self, path, **_):
        self.calls.append(path)
        return self.containers


class Gateway:
    def __init__(self):
        self.calls = []

    async def post(self, path, json=None, **_):
        self.calls.append(json)
        return {"status": 200, "ok": True, "ms": 12}


def _app(panel, slug, domains, kind="image"):
    source = {"kind": kind, "image": "redis:7"} if kind == "image" else {"kind": "compose", "compose": "services: {}"}
    spec = AppSpec.model_validate({"slug": slug, "source": source, "container_port": 6379, "domains": domains})
    app_id = deploy.create_app_row(panel, slug, 1, spec, {})
    panel.db.update("apps", app_id, status="running", host_port=20007)
    return uptime.monitor_for(panel, "app", app_id)


def test_app_without_domain_is_checked_by_container_state(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="dom", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    node = Node([
        {"name": "app-redis", "app": "redis", "state": "running", "status": "Up 2 hours"},
        {"name": "sklep-db-1", "app": "sklep", "state": "running", "status": "Up 1 hour"},
        {"name": "sklep-web-1", "app": "sklep", "state": "exited", "status": "Exited (1) 3 minutes ago"},
    ])  # fmt: skip
    gateway = Gateway()
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    monkeypatch.setattr(panel, "gateway_client", lambda: gateway)
    checker = uptime.Checker(panel)

    ok, _, message = asyncio.run(checker.check(_app(panel, "redis", [])))
    assert ok is True and message == "Up 2 hours" and not gateway.calls  # bez domeny: żadnego HTTP
    ok, _, message = asyncio.run(checker.check(_app(panel, "sklep", [], kind="compose")))
    assert ok is False and message.startswith("sklep-web-1: Exited")
    ok, _, message = asyncio.run(checker.check(_app(panel, "zniknal", [])))
    assert ok is False

    # z domeną: jak dotąd test HTTP przez bramkę
    ok, ms, _ = asyncio.run(checker.check(_app(panel, "web", ["web.example.com"])))
    assert ok is True and ms == 12 and gateway.calls[-1]["host"] == "web.example.com"
