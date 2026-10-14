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
    assert info["name"] == "testowy" and info["docker"]["Version"].startswith("28.0.0")
    vitals = api.request("GET", "/api/vitals").json()
    assert 0 <= vitals["ram"] <= 100
    assert api.request("POST", "/api/rps").json()["chosen"] in ("rock", "paper", "scissors")
    images = api.request("GET", "/api/images").json()
    assert images[0]["tags"] == ["nginx:latest"] and images[1]["dangling"] is True
    assert "deployra" in [n["name"] for n in api.request("GET", "/api/networks").json()]
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


def test_pairing_code_file_for_local_panel(tmp_path, monkeypatch):
    """panel-node.compose.yml: kod parowania leży we wspólnym wolumenie tylko do sparowania."""
    from deployra_backend import enroll
    from deployra_backend.config import Config
    from deployra_backend.context import Context
    from deployra_backend.fake_docker import FakeDocker

    code_file = tmp_path / "pair" / "code"
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_SRV", str(tmp_path / "srv"))
    monkeypatch.setenv("DEPLOYRA_PAIR_FILE", str(code_file))
    ctx = Context(Config(), docker_factory=lambda: FakeDocker())
    with TestClient(create_app(ctx, start_services=False)) as client:
        code = enroll.ensure_pairing_code(ctx)
        assert code_file.read_text() == code
        _, pub = pairing.new_keypair()
        assert client.post("/pair", json=pairing.seal(code, {"kind": "pair-node", "x25519": pub, "nonce": "n"})).status_code == 200
        assert not code_file.exists()
        assert enroll.ensure_pairing_code(ctx) is None and not code_file.exists()
        new_code = enroll.reset_pairing(ctx)
        assert code_file.read_text() == new_code


def test_sftp_port_config_and_info_ip(api, ctx):
    info = api.request("GET", "/api/info").json()
    assert "ip" in info
    res = api.request("POST", "/api/sftp/config", json={"port": 22222})
    assert res.status_code == 200
    assert res.json()["port"] == 22222
    assert ctx.state.get("sftp_port") == 22222

    sftp_info = api.request("GET", "/api/sftp").json()
    assert sftp_info["port"] == 22222

    bad = api.request("POST", "/api/sftp/config", json={"port": 999999})
    assert bad.status_code == 400


def test_wg_port_config(api, ctx):
    res = api.request("POST", "/api/wg/config", json={"port": 51825})
    assert res.status_code == 200
    assert res.json()["port"] == 51825
    assert ctx.state.get("wg_port") == 51825

    ports = api.request("GET", "/api/ports").json()
    wg_port = next((p for p in ports["used"] if p["owner"] == "WireGuard"), None)
    assert wg_port and wg_port["port"] == 51825

    bad = api.request("POST", "/api/wg/config", json={"port": 70000})
    assert bad.status_code == 400
