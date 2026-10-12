def search(api, **body):
    import time

    payload = {"since": time.time() - 3600, "until": time.time() + 5, **body}
    return api.request("POST", "/api/logs/search", json=payload)


def start(ctx, name, labels=None):
    api = ctx.docker.api
    cid = api.create_container("nginx", name=name, labels=labels or {}, host_config={})["Id"]
    api.start(cid)
    return cid


def test_search_filters_and_counts(api, ctx):
    start(ctx, "app-sklep", {"deployra.app": "sklep"})
    start(ctx, "redis")
    ctx.docker.api.log_lines["app-sklep"] = ["GET / 200", "ERROR timeout przy płatności", "GET /healthcheck 200", "error: Timeout DB"]
    ctx.docker.api.log_lines["redis"] = ["Ready to accept connections", "ERROR timeout replica"]

    data = search(api, must=["error", "timeout"]).json()
    assert data["total"] == 3 and data["scanned"] == 2 and sum(data["buckets"]) == 3
    assert {h["container"] for h in data["hits"]} == {"app-sklep", "redis"}
    assert data["hits"][0]["ts"] >= data["hits"][-1]["ts"]  # najnowsze pierwsze

    only_app = search(api, must=["error"], apps=["sklep"]).json()
    assert {h["container"] for h in only_app["hits"]} == {"app-sklep"} and only_app["scanned"] == 1
    excluded = search(api, must=["error"], exclude=["replica|DB"]).json()
    assert [h["line"] for h in excluded["hits"]] == ["ERROR timeout przy płatności"]
    everything = search(api, containers=["redis"]).json()  # bez wzorców: wszystkie linie
    assert everything["total"] == 2

    assert search(api, must=["("]).status_code == 400
    assert search(api, must=["x"], since=10, until=5).status_code == 400
