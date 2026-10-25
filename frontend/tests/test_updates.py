import time

import httpx
import pytest

from deployra_common.jobs import Job, JobFailed
from deployra_panel import __version__
from deployra_panel.services import updates

NEWER = "99.0.0"


def _github(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(request.url.path, httpx.Response(404, json={"message": "Not Found"}))

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_fetch_latest_release_tags_and_json():
    release = {"tag_name": "v1.4.0", "name": "Deployra 1.4.0", "body": "## Zmiany\n- szybciej", "html_url": "https://github.com/a/b/releases/v1.4.0",
               "published_at": "2026-10-01T10:00:00Z"}  # fmt: skip
    info = await updates.fetch_latest("a/b", _github({"/repos/a/b/releases/latest": httpx.Response(200, json=release)}))
    assert info["version"] == "1.4.0" and info["notes"].startswith("## Zmiany") and info["url"].startswith("https://")

    tags = [{"name": "v1.9.0"}, {"name": "v1.10.0"}, {"name": "nightly"}, {"name": "v1.10.0-rc1"}]
    info = await updates.fetch_latest("a/b", _github({"/repos/a/b/tags": httpx.Response(200, json=tags)}))
    assert info["version"] == "1.10.0"

    # prywatne repozytorium: API GitHuba milczy, wersję bierzemy z tagów publicznego obrazu w GHCR
    ghcr = _github({"/token": httpx.Response(200, json={"token": "anon"}),
                    "/v2/a/deployra-panel/tags/list": httpx.Response(200, json={"tags": ["1.0", "1.2.0", "latest", "1.10.0", "edge"]})})  # fmt: skip
    assert (await updates.fetch_latest("A/b", ghcr))["version"] == "1.10.0"
    with pytest.raises(ValueError, match="prywatne"):
        await updates.fetch_latest("a/b", _github({}))

    custom = _github({"/latest.json":httpx.Response(200, json={"version": "2.0.0", "notes": "x", "url": "javascript:alert(1)"})})
    info = await updates.fetch_latest("https://example.com/latest.json", custom)
    assert info["version"] == "2.0.0" and info["url"] == ""  # tylko linki http(s)


def test_validate_source():
    assert updates.validate_source("https://github.com/PanS4ge/deployra.git") == "PanS4ge/deployra"
    assert updates.validate_source(" ") == ""
    with pytest.raises(ValueError):
        updates.validate_source("to nie repo")


@pytest.mark.anyio
async def test_check_notifies_once_and_keeps_version_on_error(admin):
    panel = admin.app.state.panel
    ok = _github({"/repos/PanS4ge/deployra/releases/latest": httpx.Response(200, json={"tag_name": f"v{NEWER}"})})
    info = await updates.check(panel, ok)
    assert info["version"] == NEWER and info["error"] is None
    await updates.check(panel, ok)
    assert panel.db.scalar("SELECT COUNT(*) FROM notifications WHERE message LIKE ?", [f"%{NEWER}%"]) == 1

    limited = _github({"/repos/PanS4ge/deployra/releases/latest": httpx.Response(403, json={})})
    info = await updates.check(panel, limited)
    assert "ograniczył" in info["error"] and info["version"] == NEWER  # baner nie znika przez chwilowy błąd


def test_banner_snooze_skip(admin):
    panel = admin.app.state.panel
    assert "Dostępna aktualizacja" not in admin.get("/").text
    panel.db.set_setting("update_latest", {"version": NEWER, "name": "Deployra 99", "notes": "- <b>nowość</b>", "checked_at": time.time()})
    page = admin.get("/", headers={"HX-Request": ""}).text
    assert "Dostępna aktualizacja" in page and NEWER in page

    settings = admin.get("/settings/updates", headers={"HX-Request": ""}).text
    assert "&lt;b&gt;nowość&lt;/b&gt;" in settings  # notatki wydania są escapowane

    assert admin.post("/updates/snooze").status_code == 200
    assert "Dostępna aktualizacja" not in admin.get("/", headers={"HX-Request": ""}).text
    panel.db.set_setting("update_snooze", None)
    assert admin.post("/updates/skip", data={"version": NEWER}).status_code == 200
    assert "Dostępna aktualizacja" not in admin.get("/", headers={"HX-Request": ""}).text
    assert updates.offer(panel) is None

    assert admin.post("/settings/updates", data={"update_repo": "zła nazwa"}).status_code == 400
    assert admin.post("/settings/updates", data={"update_repo": "ktos/fork", "update_check": "1"}).status_code == 204
    assert panel.db.setting("update_repo") == "ktos/fork" and panel.db.setting("update_latest") is None


def test_start_without_updater_reports_manual(admin):
    panel = admin.app.state.panel
    panel.db.set_setting("update_latest", {"version": NEWER})
    response = admin.post("/settings/updates/start", data={"target": NEWER})
    assert response.status_code == 204 and response.headers["HX-Redirect"].startswith("/jobs/")


class FakeRemote:
    """Węzeł/bramka: po zleceniu raz „restartuje się”, potem zgłasza nową wersję (albo błąd)."""

    def __init__(self, version: str, fail: bool = False):
        self.version, self.fail, self.calls, self.polls = version, fail, [], 0

    async def post(self, path, json=None):
        self.calls.append(json["version"])
        return {"mode": "image", "state": "queued", "target": json["version"]}

    async def get(self, path, params=None):
        self.polls += 1
        if self.polls == 1:
            return {"version": self.version, "state": "running", "target": self.calls[0], "message": "pobieram obrazy"}
        if self.polls == 2:
            raise httpx.ConnectError("restart")
        if self.fail:
            return {"version": self.version, "state": "error", "target": self.calls[0], "message": "pobieranie nie powiodło się", "log": ["x"]}
        return {"version": self.calls[0], "state": "done", "target": self.calls[0]}


def _item(key, name, client, version="1.0.0"):
    return {"key": key, "name": name, "client": client, "online": True, "version": version, "update": {"available": True, "mode": "image"}}


@pytest.mark.anyio
async def test_run_order_and_failure_keeps_panel(admin, monkeypatch):
    panel = admin.app.state.panel
    monkeypatch.setattr(updates, "POLL", 0)
    node, gateway = FakeRemote("1.0.0"), FakeRemote("1.0.0", fail=True)
    me = {**_item("panel", "Panel Deployry", None, __version__), "update": {"available": True}}

    async def fake_components(_panel, log_lines=0):
        return [me, _item("gateway", "Deployra-WG", gateway), _item("node-1", "dom", node)]

    monkeypatch.setattr(updates, "components", fake_components)
    job = Job("update", "test")
    with pytest.raises(JobFailed, match="Deployra-WG"):
        await updates.run(panel, job, NEWER)
    assert node.calls == [NEWER] and gateway.calls == [NEWER]
    text = "\n".join(job.lines)
    assert "✔ dom: działa w wersji" in text and "dom: restartuje się" in text and "pobieranie nie powiodło się" in text
    assert "Panel zostaje w obecnej wersji" in text and panel.db.setting("update_pending") is None

    # wszystko się udaje → na końcu panel zleca aktualizację samemu sobie
    node, gateway = FakeRemote("1.0.0"), FakeRemote("1.0.0")
    requested = []
    monkeypatch.setattr(panel.updates, "request", lambda version: requested.append(version))
    monkeypatch.setattr(panel.updates, "info", lambda log_lines=0: {"state": "error", "message": "budowanie nie powiodło się", "log": []})
    with pytest.raises(JobFailed, match="Panel"):
        await updates.run(panel, Job("update", "test"), NEWER)
    assert requested == [NEWER] and node.calls == gateway.calls == [NEWER]


def test_finish_pending_after_restart(admin):
    panel = admin.app.state.panel
    panel.db.set_setting("update_pending", {"target": __version__, "started": time.time()})
    updates.finish_pending(panel)
    assert panel.db.setting("update_pending") is None
    assert panel.db.scalar("SELECT COUNT(*) FROM notifications WHERE message LIKE ?", ["%zaktualizowana%"]) == 1

    panel.db.set_setting("update_pending", {"target": NEWER, "started": time.time()})
    updates.finish_pending(panel)  # wciąż trwa (brak błędu, nie minęła godzina)
    assert panel.db.setting("update_pending")["target"] == NEWER
    panel.db.set_setting("update_pending", {"target": NEWER, "started": time.time() - 7200})
    updates.finish_pending(panel)
    assert panel.db.setting("update_pending") is None


def test_link_peers_finds_node_on_same_host():
    no_updater = {"available": False, "mode": None}
    panel_item = {"key": "panel", "name": "Panel", "online": True, "version": "1.2.4", "update": {**no_updater, "container": "abc"}, "address": "deploy.example.com"}
    gateway_item = {"key": "gateway", "name": "Bramka", "online": True, "version": "1.2.4", "update": no_updater, "address": "138.2.182.146"}
    node = {"key": "node-1", "name": "oracle1", "online": True, "version": "1.3.0", "update": {"available": True, "mode": "image"}, "client": object(),
            "address": "138.2.182.146", "peers": [{"id": "abc", "name": "deployra-panel", "component": "panel", "state": "idle"},
                                                  {"id": "g1", "name": "deployra-wg", "component": "gateway", "state": "idle"}]}  # fmt: skip
    other = {**node, "key": "node-2", "name": "cezary", "address": "10.0.0.2", "peers": []}
    updates._link_peers([panel_item, gateway_item, other, node])
    assert panel_item["via"]["name"] == "oracle1" and panel_item["via"]["container"] == "abc"  # po ID kontenera
    assert gateway_item["via"]["container"] == "g1"  # stara bramka bez ID — po adresie maszyny
    assert panel_item["update"]["available"] and panel_item["update"]["mode"] == "node"


class FakePeerNode:
    """Węzeł 1.3.0+ podmieniający sąsiedni kontener; `outcome` = done albo error."""

    def __init__(self, outcome="done"):
        self.outcome, self.requests, self.polls = outcome, [], 0

    async def post(self, path, json=None):
        assert path == "/api/update/peer"
        self.requests.append(json)
        return {"name": "deployra-wg", "component": "gateway", "target": json["version"]}

    async def get(self, path, params=None):
        self.polls += 1
        state = "running" if self.polls < 2 else self.outcome
        return {"peers": [{"id": "new", "name": "deployra-wg", "component": "gateway", "state": state, "target": self.requests[0]["version"],
                           "step": "pulling", "message": "", "log": ["pull failed"] if state == "error" else []}]}  # fmt: skip


class FakeGatewayVersion:
    def __init__(self):
        self.polls = 0

    async def get(self, path, params=None):
        self.polls += 1
        return {"version": NEWER if self.polls >= 3 else "1.2.4"}


@pytest.mark.anyio
async def test_gateway_and_panel_updated_by_node(admin, monkeypatch):
    panel = admin.app.state.panel
    monkeypatch.setattr(updates, "POLL", 0)
    node, gateway = FakePeerNode(), FakeGatewayVersion()
    item = {"key": "gateway", "name": "Bramka", "client": gateway, "online": True, "version": "1.2.4", "update": {"available": True, "mode": "node"},
            "via": {"key": "node-1", "name": "oracle1", "client": node, "component": "gateway", "container": "g1", "peer": "deployra-wg"}}  # fmt: skip
    job = Job("update", "test")
    assert await updates._peer(panel, job, item, NEWER)
    assert node.requests == [{"container": "g1", "version": NEWER}] and "podmienia węzeł oracle1" in "\n".join(job.lines)

    failing = FakePeerNode("error")
    me = {"key": "panel", "name": "Panel", "client": None, "online": True, "version": __version__, "update": {"available": True, "mode": "node"},
          "via": {"key": "node-1", "name": "oracle1", "client": failing, "component": "panel", "container": "p1", "peer": "deployra-wg"}}  # fmt: skip
    job = Job("update", "test")
    assert not await updates._peer(panel, job, me, NEWER)
    assert "pull failed" in "\n".join(job.lines) and panel.db.setting("update_pending") is None


def test_finish_pending_reports_rollback_by_node(admin):
    panel = admin.app.state.panel
    panel.db.set_setting("update_pending", {"target": NEWER, "started": time.time(), "via": "oracle1"})
    updates.finish_pending(panel)  # panel wstał w starej wersji → węzeł przywrócił poprzedni kontener
    assert panel.db.setting("update_pending") is None
    assert panel.db.scalar("SELECT COUNT(*) FROM notifications WHERE message LIKE ?", ["%oracle1%"]) == 1


INSPECT = {
    "Id": "b" * 64, "Name": "/deployra-backend",
    "Config": {"Image": "ghcr.io/pans4ge/deployra-backend:latest", "Env": ["DEPLOYRA_NAME=oracle1", "DEPLOYRA_LANG=pl"]},
    "Mounts": [{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
               {"Type": "bind", "Source": "/var/lib/deployra", "Destination": "/var/lib/deployra"}],
}  # fmt: skip
LISTED = [{"id": "b" * 64, "name": "deployra-backend", "image": "ghcr.io/pans4ge/deployra-backend:latest", "system": "backend", "state": "running"},
          {"id": "c1", "name": "app-blog", "image": "ghost:5-alpine", "system": None, "state": "running"}]  # fmt: skip


def test_helper_spec_copies_mounts_and_uses_new_image():
    spec = updates.helper_spec(LISTED, INSPECT, "v1.7.3")
    assert spec["image"] == "ghcr.io/pans4ge/deployra-backend:1.7.3" and spec["name"] == "deployra-backend-update"
    assert spec["command"] == ["python", "-m", "deployra_backend.services.self_update", "b" * 64, "1.7.3"]
    assert {"source": "/var/run/docker.sock", "target": "/var/run/docker.sock"} in spec["volumes"]
    assert {"source": "/var/lib/deployra", "target": "/var/lib/deployra"} in spec["volumes"] and spec["restart"] == "no"
    assert updates.backend_container(LISTED)["name"] == "deployra-backend"
    with pytest.raises(LookupError):
        updates.helper_spec(LISTED, {**INSPECT, "Config": {**INSPECT["Config"], "Image": "deployra-backend"}}, "1.7.3")  # lokalny build
    with pytest.raises(LookupError):
        updates.helper_spec(LISTED, {**INSPECT, "Mounts": INSPECT["Mounts"][1:]}, "1.7.3")  # bez docker.sock


class HelperNode:
    """Węzeł bez aktualizatora: listuje kontenery, po starcie aktualizatora restartuje się i wstaje w nowej wersji."""

    def __init__(self, outcome="ok"):
        self.outcome, self.deleted, self.version, self.polls = outcome, [], "1.0.0", 0

    async def get(self, path, params=None):
        if path == "/api/containers":
            return LISTED
        if path == f"/api/containers/{'b' * 64}":
            return INSPECT
        if path == "/api/containers/deployra-backend-update":
            return {"State": {"Status": "exited", "ExitCode": 0 if self.outcome == "ok" else 1}}
        if path == "/api/containers/deployra-backend-update/logs":
            return {"lines": ["✖ pull failed: manifest unknown"]}
        if path == "/api/update":
            self.polls += 1
            if self.outcome == "ok" and self.polls >= 2:
                return {"version": NEWER, "state": "done", "target": NEWER}
            return {"version": "1.0.0", "state": "idle", "available": False, "reason": "no_container"}
        raise AssertionError(path)

    async def delete(self, path, params=None):
        self.deleted.append(path)


@pytest.mark.anyio
async def test_node_without_updater_gets_helper(admin, monkeypatch):
    panel = admin.app.state.panel
    monkeypatch.setattr(updates, "POLL", 0)  # DONE_GRACE zostaje: aktualizator z kodem 0 to jeszcze nie porażka — węzeł wstaje
    started = []

    async def follow(client, path, body, job, prefix=""):
        started.append((path, body["image"]))
        return {"result": {"id": "h1"}}

    from deployra_panel.services import relay

    monkeypatch.setattr(relay, "start_and_follow", follow)
    node = HelperNode()
    item = {**_item("node-1", "oracle1", node), "update": {"available": True, "mode": "helper"}}

    async def fake_components(_panel, log_lines=0):
        return [item]

    monkeypatch.setattr(updates, "components", fake_components)
    job = Job("update", "test")
    await updates.run(panel, job, NEWER)
    assert started == [("/api/containers", f"ghcr.io/pans4ge/deployra-backend:{NEWER}")]
    assert node.deleted.count("/api/containers/deployra-backend-update") == 2  # pozostałość przed i po aktualizacji
    assert any(NEWER in line and "oracle1" in line for line in job.lines)

    failing = HelperNode(outcome="fail")
    item["client"] = failing
    job = Job("update", "test")
    with pytest.raises(JobFailed, match="oracle1"):
        await updates.run(panel, job, NEWER)
    assert any("manifest unknown" in line for line in job.lines)


def test_components_offer_helper_only_for_fixable_reasons():
    items = [{"key": "node-1", "online": True, "update": {"available": False, "reason": "no_container"}},
             {"key": "node-2", "online": True, "update": {"available": False, "reason": "disabled"}},
             {"key": "node-3", "online": True, "update": {"available": False}}]  # fmt: skip
    for item in items:
        update = item["update"]
        if update.get("reason", "no_updater") in updates.HELPER_REASONS:
            item["update"] = {**update, "available": True, "mode": "helper"}
    assert [i["update"].get("mode") for i in items] == ["helper", None, "helper"]
