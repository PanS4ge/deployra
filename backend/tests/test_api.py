from fastapi.testclient import TestClient

from deployra_backend.app import create_app
from deployra_common import pairing


def test_health_and_pairing_flow(ctx):
    app = create_app(ctx, start_services=False)
    with TestClient(app) as client:
        from deployra_backend import enroll

        code = enroll.ensure_pairing_code(ctx)
        health = client.get("/health").json()
        assert health["paired"] is False and health["awaiting"] == "code"

        # zły kod
        _, pub = pairing.new_keypair()
        bad = pairing.seal("DPLR-AAAA-BBBB-CCCC", {"kind": "pair-node", "x25519": pub})
        assert client.post("/pair", json=bad).status_code == 403
        # bez podpisu API nie odpowiada
        assert client.get("/api/vitals").status_code == 401

        good = pairing.seal(code, {"kind": "pair-node", "x25519": pub, "nonce": "x"})
        assert client.post("/pair", json=good).status_code == 200
        assert client.get("/health").json()["paired"] is True
        # drugie parowanie odrzucone
        assert client.post("/pair", json=good).status_code == 409


def test_pairing_lockout(ctx):
    app = create_app(ctx, start_services=False)
    with TestClient(app) as client:
        from deployra_backend import enroll

        enroll.ensure_pairing_code(ctx)
        _, pub = pairing.new_keypair()
        bad = pairing.seal("DPLR-AAAA-BBBB-CCCC", {"kind": "pair-node", "x25519": pub})
        codes = [client.post("/pair", json=bad).status_code for _ in range(12)]
        assert codes[:10] == [403] * 10 and codes[-1] == 403
        assert "zbyt wiele" in client.post("/pair", json=bad).json()["detail"]


def test_info_vitals_and_lists(api):
    info = api.request("GET", "/api/info").json()
    assert info["name"] == "testowy" and info["docker"]["Version"] == "28.0.0"
    vitals = api.request("GET", "/api/vitals").json()
    assert 0 <= vitals["ram"] <= 100
    assert api.request("POST", "/api/rps").json()["chosen"] in ("rock", "paper", "scissors")
    images = api.request("GET", "/api/images").json()
    assert images[0]["tags"] == ["nginx:latest"] and images[1]["dangling"] is True
    assert api.request("GET", "/api/networks").json()[0]["name"] == "deployra"
    assert api.request("GET", "/api/volumes").json()[0]["name"] == "dane"


def test_validation_rejects_bad_specs(api):
    response = api.request("POST", "/api/containers", json={"name": "zła nazwa!", "image": "nginx"})
    assert response.status_code == 422
    response = api.request("POST", "/api/apps/deploy", json={"slug": "x", "source": {"kind": "image", "image": "nginx"}})
    assert response.status_code == 422  # brak portu


def test_deploy_image_app_and_rollback_on_crash(api, ctx):
    spec = {"slug": "web", "source": {"kind": "image", "image": "nginx:1.27"}, "container_port": 80, "env": {"A": "1"}}
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    result = api.wait_job(job["id"])
    assert result["job"]["status"] == "ok", result["lines"]
    app = ctx.state.get("apps")["web"]
    assert 41000 <= app["host_port"] <= 41050 and app["image"] == "nginx:1.27"

    containers = api.request("GET", "/api/containers").json()
    assert [c["name"] for c in containers] == ["app-web"]
    assert containers[0]["app"] == "web" and containers[0]["ports"][0]["host"] == app["host_port"]
    stored = next(iter(ctx.docker.api.containers_.values()))
    assert stored["host_config"]["mem_limit"] is None and stored["env"] == {"A": "1"}

    # nowa wersja się wysypuje → wraca poprzednia
    ctx.docker.api.crash_images.add("nginx:broken")
    spec["source"]["image"] = "nginx:broken"
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    result = api.wait_job(job["id"])
    assert result["job"]["status"] == "error"
    assert any("Przywracam poprzednią wersję nginx:1.27" in line for line in result["lines"])
    running = [c for c in ctx.docker.api.containers_.values() if c["state"] == "running"]
    assert [c["image"] for c in running] == ["nginx:1.27"]
    # port aplikacji się nie zmienia
    assert ctx.state.get("ports")["web"] == app["host_port"]


def test_remove_app_releases_port(api, ctx):
    spec = {"slug": "api", "source": {"kind": "image", "image": "nginx"}, "container_port": 8080}
    api.wait_job(api.request("POST", "/api/apps/deploy", json=spec).json()["job"]["id"])
    assert "api" in ctx.state.get("ports")
    job = api.request("POST", "/api/apps/api/remove", json={"delete_data": True}).json()["job"]
    assert api.wait_job(job["id"])["job"]["status"] == "ok"
    assert "api" not in ctx.state.get("ports") and "api" not in ctx.state.get("apps")
    assert ctx.docker.api.containers_ == {}
