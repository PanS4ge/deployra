import asyncio

import httpx
import pytest

from deployra_common.jobs import JobFailed
from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, vulns

PLAN = {"name": "errpage", "image": "php:8.0-apache", "command": None, "env": {"TZ": "Europe/Warsaw"}, "problems": [],
        "ports": [{"container": 80, "host": 8080}], "volumes": [{"source": "/srv/errpage", "target": "/var/www/html", "read_only": False}],
        "memory_mb": None, "cpus": None}  # fmt: skip


class Node:
    def __init__(self, containers):
        self.containers, self.calls = containers, []

    async def get(self, path, **_):
        self.calls.append(path)
        if path == "/api/containers":
            return self.containers
        if path == "/api/containers/c0ffee/adopt-plan":
            return PLAN
        raise httpx.ConnectError("offline")  # zasoby i reszta API węzła — tu niepotrzebne


class Job:
    id = "j"

    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


@pytest.fixture
def app(admin):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="oracle1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    # jak po skrypcie rejestracji: tylko obraz, porty i domena — bez wolumenów z plikami strony
    spec = AppSpec.model_validate({"slug": "errpage", "source": {"kind": "image", "image": "php:8.0-apache"}, "container_port": 80, "host_port": 8080})
    app_id = deploy.create_app_row(panel, "Error Page (404)", node_id, spec, {})
    panel.db.update("apps", app_id, status="running", host_port=8080)
    return panel, app_id


def run(panel, app_id, node, monkeypatch, refresh=True):
    sent = []

    async def follow(client, path, body, job, prefix=""):
        sent.append((path, body))
        raise JobFailed("stop")  # dalej (bramka, DNS) tu niepotrzebne

    monkeypatch.setattr(panel, "node_client", lambda n: node)
    monkeypatch.setattr(deploy.relay, "start_and_follow", follow)
    job = Job()
    with pytest.raises(JobFailed):
        asyncio.run(deploy.run(panel, job, app_id, refresh=refresh))
    return sent, job


def test_registered_app_takes_over_its_container(app, monkeypatch):
    panel, app_id = app
    node = Node([{"name": "errpage", "id": "c0ffee", "app": None, "stack": None, "system": None, "ports": [{"container": 80, "host": 8080}]},
                 {"name": "inny", "app": None, "stack": None, "system": None, "ports": [{"container": 80, "host": 9000}]}])  # fmt: skip
    sent, job = run(panel, app_id, node, monkeypatch)
    path, body = sent[0]
    assert path == "/api/apps/errpage/adopt" and body["container"] == "c0ffee"  # po ID, nie po nazwie
    assert body["spec"]["volumes"] == [{"source": "/srv/errpage", "target": "/var/www/html", "read_only": False}]
    assert body["spec"]["env"] == {"TZ": "Europe/Warsaw"} and body["spec"]["refresh_image"] is True and body["spec"]["host_port"] == 8080
    assert any("errpage-pre-deployra" in line for line in job.lines)
    stored = deploy.load(panel, panel.db.get("apps", app_id))[0]
    assert stored.volumes[0].target == "/var/www/html" and stored.refresh_image is False  # uzupełniona specyfikacja zostaje


def test_managed_app_deploys_normally(app, monkeypatch):
    panel, app_id = app
    node = Node([{"name": "app-errpage", "app": "errpage", "stack": None, "system": None, "ports": [{"container": 80, "host": 8080}]}])
    sent, _ = run(panel, app_id, node, monkeypatch)
    assert sent[0][0] == "/api/apps/deploy" and not any("adopt-plan" in c for c in node.calls)


def test_ambiguous_match_does_not_guess(app, monkeypatch):
    panel, app_id = app
    node = Node([{"name": "errpage", "app": None, "stack": None, "system": None, "ports": []},
                 {"name": "inny", "app": None, "stack": None, "system": None, "ports": [{"container": 80, "host": 8080}]}])  # fmt: skip
    sent, _ = run(panel, app_id, node, monkeypatch)
    assert sent[0][0] == "/api/apps/deploy"


def test_new_app_never_takes_over_a_namesake(app, monkeypatch):
    panel, app_id = app
    panel.db.update("apps", app_id, status="new")
    node = Node([{"name": "errpage", "id": "c0ffee", "app": None, "stack": None, "system": None, "ports": [{"container": 80, "host": 8080}]}])
    sent, _ = run(panel, app_id, node, monkeypatch)
    assert sent[0][0] == "/api/apps/deploy" and "/api/containers" not in node.calls


def test_blocked_takeover_is_clear(app, monkeypatch):
    panel, app_id = app
    PLAN["problems"] = ["kontener należy do stacka compose"]
    try:
        node = Node([{"name": "errpage", "id": "c0ffee", "app": None, "stack": None, "system": None, "ports": [{"container": 80, "host": 8080}]}])
        monkeypatch.setattr(panel, "node_client", lambda n: node)
        job = Job()
        with pytest.raises(JobFailed) as exc:
            asyncio.run(deploy.run(panel, job, app_id))
        assert "errpage" in str(exc.value) and "compose" in str(exc.value)
    finally:
        PLAN["problems"] = []


def test_new_branch_hint():
    assert vulns.new_branch("8.0.30", "8.2.35") and not vulns.new_branch("8.3.12", "8.3.36") and not vulns.new_branch("8.0.30", None)
