import asyncio
import json

import httpx
import pytest

from deployra_common.schemas import AppSpec
from deployra_panel.services import app_updates, deploy

OLD, NEW = "sha256:" + "1" * 64, "sha256:" + "2" * 64


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(app_updates, "RETRY_DELAY", 0)


def test_refs_tags_and_versions():
    ref = app_updates.parse_ref("nginx:1.27")
    assert (ref.registry, ref.repo, ref.tag) == ("registry-1.docker.io", "library/nginx", "1.27")
    assert app_updates.parse_ref("ghcr.io/Owner/App").repo == "owner/app" and app_updates.parse_ref("ghcr.io/owner/app").tag == "latest"
    assert app_updates.parse_ref("localhost:5000/team/app:2.0.1").registry == "localhost:5000"
    assert app_updates.parse_ref("nginx@sha256:abc") is None
    assert app_updates.with_tag("localhost:5000/app:1.0.0", "1.0.1") == "localhost:5000/app:1.0.1"
    assert app_updates.with_tag("grafana/grafana:11.2.0", "11.3.0") == "grafana/grafana:11.3.0"
    assert app_updates.pinned("29.0.7-apache") and not app_updates.pinned("8.3-apache") and not app_updates.pinned("latest")
    tags = ["11.2.0", "11.2.3", "11.3.1", "12.0.0", "12.1.0-beta1", "11.4.0-ubuntu", "latest", "v11.9.0"]
    assert app_updates.newer("11.2.0", tags) == {"patch": "11.2.3", "minor": "11.3.1", "latest": "12.0.0"}
    assert app_updates.newer("29.0.7-apache", ["29.0.9-apache", "29.0.9", "30.0.1-fpm"]) == {"patch": "29.0.9-apache", "minor": "29.0.9-apache",
                                                                                          "latest": "29.0.9-apache"}  # fmt: skip
    assert app_updates.newer("12.0.0", tags) is None and app_updates.newer("8.3-apache", tags) is None


class Hub:
    """Atrapa rejestru: HEAD manifestu za tokenem Bearer, lista tagów stronami (Link)."""

    def __init__(self):
        self.digests = {("library/php", "8.3-apache"): NEW, ("grafana/grafana", "11.2.0"): OLD, ("library/redis", "7-alpine"): OLD}
        self.tags = {"grafana/grafana": [["11.2.0", "11.2.2"], ["11.3.0", "12.0.0"]]}
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.host == "auth.docker.io":
            assert request.url.params["service"] == "registry.docker.io" and request.url.params["scope"].startswith("repository:")
            return httpx.Response(200, json={"token": "t0k"})
        if request.headers.get("authorization") != "Bearer t0k":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'})
        parts = request.url.path.split("/")
        if "manifests" in parts:
            i = parts.index("manifests")
            repo, tag = "/".join(parts[2:i]), parts[i + 1]
            assert "application/vnd.oci.image.index.v1+json" in request.headers["accept"]
            digest = self.digests.get((repo, tag))
            return httpx.Response(200, headers={"Docker-Content-Digest": digest}) if digest else httpx.Response(404)
        repo = "/".join(parts[2:-2])
        pages = self.tags.get(repo, [[]])
        page = int(request.url.params.get("page", 0))
        headers = {"Link": f'</v2/{repo}/tags/list?n=1000&page={page + 1}>; rel="next"'} if page + 1 < len(pages) else {}
        return httpx.Response(200, json={"tags": pages[page]}, headers=headers)

    def transport(self):
        return httpx.MockTransport(self.handler)


class Node:
    def __init__(self):
        self.containers = [
            {"name": "app-sklep", "app": "sklep", "image": "php:8.3-apache", "digests": [OLD], "base": True, "state": "running"},
            {"name": "app-grafana", "app": "grafana", "image": "grafana/grafana:11.2.0", "digests": [OLD], "base": False, "state": "running"},
            {"name": "app-cache", "app": "cache", "image": "redis:7-alpine", "digests": [OLD], "base": False, "state": "running"},
        ]

    async def get(self, path, **_):
        if path != "/api/images/digests":
            raise httpx.ConnectError("offline")  # zasoby i reszta API węzła — tu niepotrzebne
        return {"endpoints": [{"id": "local", "name": "Host", "error": None, "containers": self.containers}]}


@pytest.fixture
def setup(admin, monkeypatch):
    from deployra_panel.services import resources

    monkeypatch.setattr(resources, "LATEST", {})
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="dom", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    ids = {}
    for slug, source in (("sklep", {"kind": "runtime", "runtime": "php"}), ("grafana", {"kind": "image", "image": "grafana/grafana:11.2.0"}),
                         ("cache", {"kind": "image", "image": "redis:7-alpine"})):  # fmt: skip
        ids[slug] = deploy.create_app_row(panel, slug.title(), node_id, AppSpec.model_validate({"slug": slug, "source": source, "container_port": 3000}), {})
    node, hub = Node(), Hub()
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    return panel, node, hub, ids


def run(panel, hub, job=None):
    return asyncio.run(app_updates.check(panel, job, transport=hub.transport()))


def test_check_finds_new_build_and_new_version(setup):
    panel, _, hub, ids = setup
    result = run(panel, hub)
    assert result["error"] is None and result["images"] == 3 and result["updates"] == 2
    sklep, grafana = app_updates.available(panel, ids["sklep"]), app_updates.available(panel, ids["grafana"])
    assert sklep == [{"container": "app-sklep", "image": "php:8.3-apache", "base": True, "build": True, "remote": NEW, "versions": None}]
    assert grafana[0]["build"] is False and grafana[0]["versions"] == {"patch": "11.2.2", "minor": "11.3.0", "latest": "12.0.0"}
    assert app_updates.available(panel, ids["cache"]) == []  # ten sam digest
    assert app_updates.badges(panel) == {ids["sklep"]: {"kind": "build", "label": "", "auto": False},
                                         ids["grafana"]: {"kind": "version", "label": "11.3.0", "auto": False}}  # fmt: skip
    assert ("GET", "/v2/grafana/grafana/tags/list") in hub.calls  # druga strona tagów przez nagłówek Link też przeczytana
    events = panel.db.fetchall("SELECT * FROM notifications")
    assert any("Sklep" in e["message"] for e in events) and any("11.3.0" in e["message"] for e in events)

    count = len(panel.db.fetchall("SELECT * FROM notifications"))
    run(panel, hub)  # to samo — bez drugiego powiadomienia
    assert len(panel.db.fetchall("SELECT * FROM notifications")) == count

    hub.digests[("library/php", "8.3-apache")] = OLD
    run(panel, hub)
    assert app_updates.available(panel, ids["sklep"]) == []


def test_policy_off_hides_badge_and_per_app_override(setup, admin):
    panel, _, hub, ids = setup
    run(panel, hub)
    assert admin.post(f"/apps/{ids['grafana']}/update-policy", data={"policy": "off"}).status_code == 200
    assert ids["grafana"] not in app_updates.badges(panel)
    assert admin.post(f"/apps/{ids['grafana']}/update-policy", data={"policy": "bogus"}).status_code == 400
    assert admin.post("/settings/app-updates", data={"mode": "off", "interval_hours": "6"}).status_code == 200
    assert app_updates.badges(panel) == {}  # sklep dziedziczy „off” z ustawień
    assert admin.post("/settings/app-updates", data={"mode": "notify", "interval_hours": "5"}).status_code == 400


def test_badge_on_icons_and_app_page(setup, admin):
    panel, _, hub, ids = setup
    run(panel, hub)
    apps = admin.get("/apps", headers={"HX-Request": ""}).text
    assert apps.count('class="app-up') == 2
    detail = admin.get(f"/apps/{ids['grafana']}", headers={"HX-Request": ""}).text
    assert "grafana/grafana:11.2.0: nowa wersja 11.3.0" in detail and f'hx-post="/apps/{ids["grafana"]}/update"' in detail
    page = admin.get("/settings/app-updates", headers={"HX-Request": ""}).text
    assert "11.3.0" in page and "php:8.3-apache" in page and "/settings/app-updates/all" in page
    assert page.count('/update" hx-swap') == 2 and f'hx-post="/apps/{ids["cache"]}/update"' not in page  # aktualna — bez przycisku


def test_auto_mode_updates_builds_and_patch_versions(setup, monkeypatch):
    panel, _, hub, ids = setup
    app_updates.save_settings(panel, "auto", 12, auto_patch=True)
    calls = []

    async def fake_run(panel_, job, app_id, adopt_container=None, refresh=False, **_):
        calls.append((app_id, refresh, deploy.load(panel, panel.db.get("apps", app_id))[0].source.image))
        app_updates.after_deploy(panel, panel.db.get("apps", app_id), refresh)
        return {}

    monkeypatch.setattr(deploy, "run", fake_run)

    class Job:
        lines = []

        def log(self, line):
            self.lines.append(line)

    run(panel, hub, Job())
    assert sorted(calls) == sorted([(ids["sklep"], True, None), (ids["grafana"], True, "grafana/grafana:11.2.2")])
    assert app_updates.available(panel, ids["sklep"]) == []
    # zostaje wersja spoza linii poprawkowej — tej sam nie zmienię
    assert app_updates.available(panel, ids["grafana"]) == []  # wpis obrazu ze starym tagiem znika po zmianie tagu
    titles = [n["message"] for n in panel.db.fetchall("SELECT * FROM notifications")]
    assert any("zaktualizowano" in m for m in titles)


def test_manual_update_changes_tag_and_compose(setup, admin, monkeypatch):
    panel, _, hub, ids = setup
    run(panel, hub)
    seen = []

    async def fake_update(panel_, job, app_id, changes=None):
        seen.append((app_id, changes))

    monkeypatch.setattr(app_updates, "update_app", fake_update)
    response = admin.post(f"/apps/{ids['grafana']}/update")
    assert response.status_code == 204 and response.headers["HX-Redirect"].startswith(f"/apps/{ids['grafana']}?job=")
    job = panel.jobs.get(response.headers["HX-Redirect"].split("job=")[1])
    for _ in range(100):
        if job.done:
            break
        asyncio.run(asyncio.sleep(0.02))
    assert seen == [(ids["grafana"], {"grafana/grafana:11.2.0": "grafana/grafana:11.3.0"})]
    assert admin.post(f"/apps/{ids['cache']}/update").status_code == 200  # nie ma czego aktualizować (toast)

    compose_spec = AppSpec.model_validate({"slug": "stos", "container_port": 3000, "source": {"kind": "compose", "compose":
        "services:\n  web:\n    image: grafana/grafana:11.2.0  # panel\n  db:\n    image: \"postgres:16.4\"\n"}})  # fmt: skip
    stos = deploy.create_app_row(panel, "Stos", 1, compose_spec, {})
    app_updates.apply_versions(panel, panel.db.get("apps", stos), {"grafana/grafana:11.2.0": "grafana/grafana:11.3.0", "postgres:16.4": "postgres:16.6"})
    compose = deploy.load(panel, panel.db.get("apps", stos))[0].source.compose
    assert "image: grafana/grafana:11.3.0  # panel" in compose and 'image: "postgres:16.6"' in compose


def test_registry_credentials_and_errors(setup):
    panel, node, hub, ids = setup
    from deployra_panel.services import registries

    registries.add_registry(panel, "Prywatny", "registry.example.com", "robot", "s3cret")
    node.containers = [{"name": "app-cache", "app": "cache", "image": "registry.example.com/team/cache:1.0.0", "digests": [OLD], "base": False}]
    seen = {}

    def private(request):
        if request.url.host == "registry.example.com":
            if "authorization" not in request.headers:
                return httpx.Response(401, headers={"www-authenticate": 'Basic realm="registry"'})
            seen["auth"] = request.headers["authorization"]
            if "manifests" in request.url.path:
                return httpx.Response(200, headers={"Docker-Content-Digest": NEW})
            return httpx.Response(500)  # lista tagów nie działa — digest i tak się liczy
        return hub.handler(request)

    result = asyncio.run(app_updates.check(panel, transport=httpx.MockTransport(private)))
    assert seen["auth"].startswith("Basic ") and result["updates"] == 0  # błąd tagów → bez wyniku dla obrazu, ale skan się kończy
    assert result["error"] is None


def test_old_node_is_reported(setup):
    panel, node, hub, _ = setup
    from deployra_common.client import RemoteError

    async def old(path, **_):
        raise RemoteError(404, "Not Found")

    node.get = old
    result = run(panel, hub)
    assert "zaktualizuj" in next(iter(result["nodes"].values()))["error"]


def wait(panel, response):
    job = panel.jobs.get(response.headers["HX-Redirect"].split("job=")[1])
    for _ in range(200):
        if job.done:
            break
        asyncio.run(asyncio.sleep(0.02))
    return job


def test_ignore_hides_until_newer_build(setup, admin):
    panel, _, hub, ids = setup
    run(panel, hub)
    assert admin.post("/settings/app-updates/ignore", data={"app_ids": [str(ids["sklep"]), str(ids["grafana"])]}).status_code == 204
    assert app_updates.badges(panel) == {} and app_updates.available(panel, ids["sklep"]) == []
    page = admin.get("/settings/app-updates", headers={"HX-Request": ""}).text
    assert page.count("bi-eye-slash\"></i> zignorowana") == 2 and f"/settings/app-updates/{ids['sklep']}/unignore" in page
    run(panel, hub)  # ta sama aktualizacja — dalej zignorowana
    assert app_updates.available(panel, ids["sklep"]) == []
    hub.digests[("library/php", "8.3-apache")] = "sha256:" + "3" * 64  # kolejny build — pokazuje się znowu
    run(panel, hub)
    assert app_updates.available(panel, ids["sklep"])[0]["remote"].endswith("3" * 64)
    assert ids["grafana"] not in app_updates.badges(panel)
    assert admin.post(f"/settings/app-updates/{ids['grafana']}/unignore").status_code == 204
    assert ids["grafana"] in app_updates.badges(panel)


def test_update_selected_apps_in_order(setup, admin, monkeypatch):
    panel, _, hub, ids = setup
    run(panel, hub)
    seen = []

    async def fake_update(panel_, job, app_id, changes=None):
        seen.append(app_id)

    monkeypatch.setattr(app_updates, "update_app", fake_update)
    wait(panel, admin.post("/settings/app-updates/all", data={"app_ids": [str(ids["grafana"])]}))
    assert seen == [ids["grafana"]]
    seen.clear()
    wait(panel, admin.post("/settings/app-updates/all"))
    assert seen == sorted([ids["sklep"], ids["grafana"]])
