from deployra_backend.services import apps


def deploy(api, spec):
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    return api.wait_job(job["id"])


def spec(image, **extra):
    return {"slug": "errpage", "source": {"kind": "image", "image": image}, "container_port": 80, "host_port": 45996, **extra}


def running_image(ctx):
    return next(c["image"] for c in ctx.docker.api.containers_.values() if c["name"] == "app-errpage")


def test_unhealthy_new_version_goes_back(api, ctx, monkeypatch):
    ctx.health_timeout, ctx.health_interval = 0.3, 0.05
    assert deploy(api, spec("php:8.0-apache"))["job"]["status"] == "ok"
    monkeypatch.setattr(apps, "http_status", lambda url: 500)  # PHP 8.2 wywala stary kod
    result = deploy(api, spec("php:8.2-apache", verify=True, cleanup=True))
    assert result["job"]["status"] == "error"
    assert running_image(ctx) == "php:8.0-apache"  # wróciła poprzednia wersja
    assert any("test HTTP" in line or "HTTP test" in line for line in result["lines"])


def test_healthy_new_version_removes_old_image(api, ctx, monkeypatch):
    ctx.health_timeout, ctx.health_interval = 0.3, 0.05
    assert deploy(api, spec("php:8.0-apache"))["job"]["status"] == "ok"
    removed = []
    monkeypatch.setattr(ctx.docker.api, "remove_image", lambda image, force=False: removed.append(image))
    monkeypatch.setattr(apps, "http_status", lambda url: 404)  # strona błędu 404 — działa
    result = deploy(api, spec("php:8.2-apache", verify=True, cleanup=True))
    assert result["job"]["status"] == "ok", result["lines"]
    assert running_image(ctx) == "php:8.2-apache" and removed == ["php:8.0-apache"]


def test_takeover_with_cleanup_removes_backup(api, ctx, monkeypatch):
    ctx.health_timeout, ctx.health_interval = 0.3, 0.05
    monkeypatch.setattr(apps, "http_status", lambda url: 200)
    cid = ctx.docker.api.create_container("php:8.0-apache", name="errpage", host_config={"port_bindings": {"80/tcp": 45997}})["Id"]
    ctx.docker.api.start(cid)
    body = {"container": cid, "spec": {**spec("php:8.2-apache", host_port=45997), "verify": True, "cleanup": True}}
    result = api.wait_job(api.request("POST", "/api/apps/errpage/adopt", json=body).json()["job"]["id"])
    assert result["job"]["status"] == "ok", result["lines"]
    names = {c["name"] for c in ctx.docker.api.containers_.values()}
    assert "app-errpage" in names and "errpage-pre-deployra" not in names and "errpage" not in names
    assert result["job"]["result"]["backup"] is None
