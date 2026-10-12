from test_resources import FakeNode, job_of, nodes, wait_job  # noqa: F401 — fixture `nodes`

PLAN = {
    "name": "moj-redis", "image": "redis:7-alpine", "command": ["redis-server", "--appendonly", "yes"], "env": {"TZ": "Europe/Warsaw"},
    "ports": [{"container": 6379, "host": 26379}], "volumes": [{"source": "redis-data", "target": "/data", "read_only": False}],
    "memory_mb": 256, "cpus": None, "networks": ["bridge", "stary-stack_default"], "state": "running", "problems": [],
}  # fmt: skip


def with_plan(node: FakeNode, plan: dict) -> None:
    original = node.get

    async def get(path, **kwargs):
        if path.endswith("/adopt-plan"):
            node.calls.append(("GET", path))
            return plan
        return await original(path, **kwargs)

    node.get = get


def test_adopt_page_and_flow(admin, nodes):
    panel = admin.app.state.panel
    with_plan(nodes[1], PLAN)
    page = admin.get("/nodes/1/containers/abc123/adopt", headers={"HX-Request": ""}).text
    assert "redis:7-alpine" in page and "redis-server --appendonly yes" in page and "stary-stack_default" in page

    response = admin.post("/nodes/1/containers/abc123/adopt", data={"name": "Redis", "slug": "redis", "port": "6379",
                                                                     "domains": "", "https": "1"})  # fmt: skip
    assert response.status_code == 204, response.headers.get("HX-Trigger")
    job = wait_job(panel, job_of(response))
    assert job.status == "ok", job.lines
    call = next(c for c in nodes[1].calls if c[0] == "JOB" and c[1] == "/api/apps/redis/adopt")
    spec = call[2]["spec"]
    assert call[2]["container"] == "abc123" and spec["host_port"] == 26379 and spec["container_port"] == 6379
    assert spec["source"]["command"] == ["redis-server", "--appendonly", "yes"] and spec["env"] == {"TZ": "Europe/Warsaw"}
    assert spec["volumes"][0]["source"] == "redis-data" and spec["memory_mb"] == 256
    app = panel.db.get("apps", "redis", key="slug")
    assert app["status"] == "running" and app["kind"] == "image"
    # polecenie widać w ustawieniach aplikacji i przeżywa zapis
    assert "redis-server --appendonly yes" in admin.get(f"/apps/{app['id']}/edit", headers={"HX-Request": ""}).text


def test_adopt_refuses_problems(admin, nodes):
    with_plan(nodes[1], {**PLAN, "problems": ["Kontener należy do stacka compose"]})
    response = admin.post("/nodes/1/containers/abc123/adopt", data={"name": "x"})
    assert response.status_code == 400
    assert not any(c[0] == "JOB" for c in nodes[1].calls)
