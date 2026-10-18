"""Wdrożenie bez przestojów: nowa wersja obok starej, test zdrowia, przełączenie po stronie panelu i sprzątanie."""

import pytest

from deployra_backend.services import apps


def app_spec(image="nginx:1.27", **extra):
    return {"slug": "web", "source": {"kind": "image", "image": image}, "container_port": 80, **extra}


@pytest.fixture
def fast(ctx, monkeypatch):
    """Krótkie limity czasu i bez czekania po przełączeniu; odpowiedź testu zdrowia sterujemy z testu."""
    ctx.health_timeout, ctx.health_interval, ctx.handover_grace = 0.3, 0.05, 0
    answer = {"status": 200, "urls": []}

    def probe(url):
        answer["urls"].append(url)
        return answer["status"]

    monkeypatch.setattr(apps, "http_status", probe)
    return answer


def run(api, path, body=None):
    response = api.request("POST", path, json=body) if body is not None else api.request("POST", path)
    return api.wait_job(response.json()["job"]["id"])


def deploy(api, spec):
    return run(api, "/api/apps/deploy", spec)


def commit(api, slug="web"):
    return run(api, f"/api/apps/{slug}/handover/commit")


def abort(api, slug="web"):
    return run(api, f"/api/apps/{slug}/handover/abort")


def names(ctx):
    return sorted((c["name"], c["image"], c["state"]) for c in ctx.docker.api.containers_.values())


def test_zero_downtime_success_path(api, ctx, fast):
    first = deploy(api, app_spec(zero_downtime=True, health_path="/up"))
    assert first["job"]["status"] == "ok", first["lines"]
    handover = first["job"]["result"]
    assert handover["handover"] is True and 41000 <= handover["host_port"] <= 41050
    # nowy kontener startuje jako app-web-next, a bramka i stan aplikacji jeszcze o nim nie wiedzą
    assert names(ctx) == [("app-web-next", "nginx:1.27", "running")]
    assert ctx.state.get("ports") == {"web#next": handover["host_port"]}
    assert fast["urls"] and all(u == f"http://127.0.0.1:{handover['host_port']}/up" for u in fast["urls"])

    result = commit(api)
    assert result["job"]["status"] == "ok", result["lines"]
    assert names(ctx) == [("app-web", "nginx:1.27", "running")]
    app = ctx.state.get("apps")["web"]
    assert app["host_port"] == handover["host_port"] and "handover" not in app
    assert ctx.state.get("ports") == {"web": handover["host_port"]}

    # druga wersja: stara działa, dopóki bramka nie przełączy ruchu
    second = deploy(api, app_spec("nginx:1.28", zero_downtime=True, health_path="/up"))
    assert second["job"]["status"] == "ok", second["lines"]
    new_port = second["job"]["result"]["host_port"]
    assert new_port != handover["host_port"]
    assert names(ctx) == [("app-web", "nginx:1.27", "running"), ("app-web-next", "nginx:1.28", "running")]
    assert ctx.state.get("apps")["web"]["image"] == "nginx:1.27"
    assert ctx.state.get("apps")["web"]["host_port"] == handover["host_port"]

    assert commit(api)["job"]["status"] == "ok"
    assert names(ctx) == [("app-web", "nginx:1.28", "running")]
    assert ctx.state.get("apps")["web"]["host_port"] == new_port and ctx.state.get("ports") == {"web": new_port}


def test_zero_downtime_without_health_path_only_checks_running(api, ctx, fast):
    assert deploy(api, app_spec(zero_downtime=True))["job"]["status"] == "ok"
    assert fast["urls"] == []
    assert commit(api)["job"]["status"] == "ok"
    assert names(ctx) == [("app-web", "nginx:1.27", "running")]


def test_unhealthy_new_container_keeps_old_one(api, ctx, fast):
    assert deploy(api, app_spec(zero_downtime=True, health_path="/up"))["job"]["status"] == "ok"
    assert commit(api)["job"]["status"] == "ok"
    before = ctx.state.get("apps")["web"]

    fast["status"] = 503  # nowa wersja odpowiada błędem serwera
    job = deploy(api, app_spec("nginx:1.28", zero_downtime=True, health_path="/up"))
    assert job["job"]["status"] == "error"
    assert any("nie odpowiada" in line and "503" in line for line in job["lines"]), job["lines"]
    assert names(ctx) == [("app-web", "nginx:1.27", "running")]  # stara wersja nietknięta, nowa usunięta
    assert ctx.state.get("apps")["web"] == before
    assert ctx.state.get("ports") == {"web": before["host_port"]}


def test_new_container_that_crashes_keeps_old_one(api, ctx, fast):
    assert deploy(api, app_spec(zero_downtime=True))["job"]["status"] == "ok"
    assert commit(api)["job"]["status"] == "ok"
    ctx.docker.api.crash_images.add("nginx:broken")
    job = deploy(api, app_spec("nginx:broken", zero_downtime=True))
    assert job["job"]["status"] == "error"
    assert names(ctx) == [("app-web", "nginx:1.27", "running")]
    assert ctx.state.get("ports") == {"web": ctx.state.get("apps")["web"]["host_port"]}


def test_abort_removes_new_version_and_keeps_old(api, ctx, fast):
    assert deploy(api, app_spec(zero_downtime=True))["job"]["status"] == "ok"
    assert commit(api)["job"]["status"] == "ok"
    assert deploy(api, app_spec("nginx:1.28", zero_downtime=True))["job"]["status"] == "ok"
    assert names(ctx) == [("app-web", "nginx:1.27", "running"), ("app-web-next", "nginx:1.28", "running")]

    job = abort(api)
    assert job["job"]["status"] == "ok", job["lines"]
    assert names(ctx) == [("app-web", "nginx:1.27", "running")]
    assert "handover" not in ctx.state.get("apps")["web"] and ctx.state.get("ports") == {"web": ctx.state.get("apps")["web"]["host_port"]}
    # bez oczekującego wdrożenia zatwierdzenie się nie udaje
    assert commit(api)["job"]["status"] == "error"


def test_rollback_after_zero_downtime_deploy(api, ctx, fast):
    assert deploy(api, app_spec(zero_downtime=True))["job"]["status"] == "ok"
    assert commit(api)["job"]["status"] == "ok"
    assert deploy(api, app_spec("nginx:1.28", zero_downtime=True))["job"]["status"] == "ok"
    assert commit(api)["job"]["status"] == "ok"
    port = ctx.state.get("apps")["web"]["host_port"]

    job = run(api, "/api/apps/web/rollback", app_spec("nginx:1.28", zero_downtime=True))
    assert job["job"]["status"] == "ok", job["lines"]
    assert names(ctx) == [("app-web", "nginx:1.27", "running")]
    assert ctx.state.get("apps")["web"]["host_port"] == port


def test_remove_cleans_up_pending_handover(api, ctx, fast):
    assert deploy(api, app_spec(zero_downtime=True))["job"]["status"] == "ok"
    job = run(api, "/api/apps/web/remove", {"delete_data": True})
    assert job["job"]["status"] == "ok", job["lines"]
    assert ctx.docker.api.containers_ == {} and ctx.state.get("ports") == {}


def test_switch_off_keeps_classic_replacement(api, ctx, fast, monkeypatch):
    """Bez opcji: stary kontener znika przed startem nowego, bez testu HTTP i bez przełączenia."""

    def no_probe(url):
        raise AssertionError("test zdrowia nie powinien działać przy wyłączonej opcji")

    monkeypatch.setattr(apps, "http_status", no_probe)
    first = deploy(api, app_spec(health_path="/up"))
    assert first["job"]["status"] == "ok" and "handover" not in first["job"]["result"]
    result = deploy(api, app_spec("nginx:1.28", health_path="/up"))
    assert result["job"]["status"] == "ok" and "handover" not in result["job"]["result"]
    assert names(ctx) == [("app-web", "nginx:1.28", "running")]
    assert ctx.state.get("apps")["web"]["host_port"] == result["job"]["result"]["host_port"]
    assert "web#next" not in ctx.state.get("ports")
