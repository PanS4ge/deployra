from deployra_backend.services import ports


def deploy(api, spec):
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    return api.wait_job(job["id"])


def spec(**extra):
    return {"slug": "errpage", "source": {"kind": "image", "image": "nginx:latest"}, "container_port": 80, **extra}


def busy(ctx, name, port):
    cid = ctx.docker.api.create_container("redis:7-alpine", name=name, host_config={"port_bindings": {"6379/tcp": port}})["Id"]
    ctx.docker.api.start(cid)


def test_own_container_keeps_its_port_without_state(api, ctx):
    assert deploy(api, spec(host_port=45991))["job"]["status"] == "ok"
    ctx.state.mutate("ports", lambda p: {})  # np. przejęty kontener albo stan węzła odtworzony z kopii
    result = deploy(api, {**spec(host_port=45991), "refresh_image": True})  # „Napraw” z podatności
    assert result["job"]["status"] == "ok", result["lines"]
    assert ctx.state.get("ports")["errpage"] == 45991


def test_remembered_port_taken_moves_to_free_one(api, ctx):
    assert deploy(api, spec())["job"]["status"] == "ok"
    old = ctx.state.get("ports")["errpage"]
    ctx.state.mutate("ports", lambda p: {})
    api.request("POST", "/api/apps/errpage/power", json={"kind": "image", "running": False})
    ctx.docker.api.remove_container("app-errpage", force=True)
    busy(ctx, "obcy", old)
    result = deploy(api, spec())
    assert result["job"]["status"] == "ok", result["lines"]
    new = ctx.state.get("ports")["errpage"]
    assert new != old and any(str(old) in line and str(new) in line for line in result["lines"])


def test_explicit_port_taken_is_a_clear_error(api, ctx):
    busy(ctx, "panel-obok", 45992)
    result = deploy(api, spec(host_port=45992))
    assert result["job"]["status"] == "error"
    text = "\n".join(result["lines"])
    assert "Traceback" not in text and "45992" in text and "panel-obok" in text
    assert "45992" in result["job"]["error"]


def test_holder_names_other_app(ctx):
    ctx.state.mutate("ports", lambda p: {"blog": 45993})
    assert "blog" in ports.holder(ctx.state, ctx.docker, "errpage", 45993)
