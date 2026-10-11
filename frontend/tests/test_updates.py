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
