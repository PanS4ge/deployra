def run(api, method, path, json=None):
    response = api.request(method, path, json=json)
    assert response.status_code == 200, response.text
    return api.wait_job(response.json()["job"]["id"])


def make_container(ctx, **extra):
    api = ctx.docker.api
    cid = api.create_container("redis:7-alpine", command=["redis-server", "--appendonly", "yes"], name="moj-redis",
                               environment={"TZ": "Europe/Warsaw"}, labels=extra.pop("labels", {}),
                               host_config=api.create_host_config(port_bindings={"6379/tcp": 26379}, mem_limit="256m",
                                                                  restart_policy={"Name": "always"}))["Id"]  # fmt: skip
    api.start(cid)
    return cid


def test_plan_reads_container(api, ctx):
    cid = make_container(ctx)
    plan = api.request("GET", f"/api/containers/{cid}/adopt-plan").json()
    assert plan["image"] == "redis:7-alpine" and plan["command"] == ["redis-server", "--appendonly", "yes"]
    assert plan["env"] == {"TZ": "Europe/Warsaw"} and plan["ports"] == [{"container": 6379, "host": 26379}]
    assert plan["memory_mb"] == 256 and plan["volumes"][0]["source"] == "dane" and plan["problems"] == []


def test_adopt_replaces_container_and_keeps_backup(api, ctx):
    cid = make_container(ctx)
    spec = {"slug": "redis", "source": {"kind": "image", "image": "redis:7-alpine", "command": ["redis-server", "--appendonly", "yes"]},
            "container_port": 6379, "host_port": 26379, "env": {"TZ": "Europe/Warsaw"}, "memory_mb": 256,
            "volumes": [{"source": "dane", "target": "/data"}]}  # fmt: skip
    result = run(api, "POST", "/api/apps/redis/adopt", {"container": cid, "spec": spec})
    assert result["job"]["status"] == "ok", result["lines"]
    containers = {c["name"]: c for c in ctx.docker.api.containers_.values()}
    assert containers["moj-redis-pre-deployra"]["state"] == "exited"
    assert containers["moj-redis-pre-deployra"]["host_config"]["restart_policy"] == {"Name": "no"}
    new = containers["app-redis"]
    assert new["command"] == ["redis-server", "--appendonly", "yes"] and new["ports"] == {"6379/tcp": 26379}
    assert new["host_config"]["binds"]["dane"]["bind"] == "/data"
    assert ctx.state.get("apps")["redis"]["host_port"] == 26379
    assert api.request("GET", f"/api/containers/{new['id']}/adopt-plan").json()["problems"]  # już aplikacja


def test_failed_adopt_restores_original(api, ctx):
    cid = make_container(ctx)
    ctx.docker.api.crash_images.add("redis:broken")
    spec = {"slug": "redis", "source": {"kind": "image", "image": "redis:broken"}, "container_port": 6379}
    result = run(api, "POST", "/api/apps/redis/adopt", {"container": cid, "spec": spec})
    assert result["job"]["status"] == "error"
    original = ctx.docker.api.containers_[cid]
    assert original["name"] == "moj-redis" and original["state"] == "running"
    assert original["host_config"]["restart_policy"] == {"Name": "always"}


def test_compose_and_system_containers_are_refused(api, ctx):
    cid = make_container(ctx, labels={"com.docker.compose.project": "nextcloud"})
    assert api.request("GET", f"/api/containers/{cid}/adopt-plan").json()["problems"]
    spec = {"slug": "nc", "source": {"kind": "image", "image": "redis:7-alpine"}, "container_port": 6379}
    assert api.request("POST", "/api/apps/nc/adopt", json={"container": cid, "spec": spec}).status_code == 400
