import time

from deployra_common.client import RemoteError
from deployra_panel.services import analytics, proxy_health, proxy_state


class FakeGateway:
    def __init__(self, batch=True):
        self.batch, self.posts, self.gets = batch, [], 0
        self.get_paths = []

    async def post(self, path, json=None, **_):
        self.posts.append((path, json))
        if path == "/api/check/batch":
            if not self.batch:
                raise RemoteError(404, "Not Found")
            return {"results": {i["id"]: {"ok": True, "status": 200, "ms": 12} for i in json["items"]}}
        return {"ok": False, "status": None, "error": "connection refused"}

    async def get(self, path, **_):
        self.gets += 1
        self.get_paths.append(path)
        return {"domains": {"a.example.com": [{"day": "2026-10-12", "requests": 5, "uniques": 3, "returning": 1, "bytes": 10}]}}


def add_sites(panel, count=3):
    return [panel.db.insert("sites", kind="proxy", domains=[f"a{i}.example.com" if i else "a.example.com"], enabled=1,
                            target={"type": "address", "host": "130.162.245.37", "port": 8080 + i, "scheme": "http", "path": ""})
            for i in range(count)]  # fmt: skip


def test_proxy_list_loads_health_and_sparks_in_batches(admin, monkeypatch):
    panel = admin.app.state.panel
    proxy_health._cache.clear()
    analytics._cache.clear()
    proxy_state._EXTERNAL_CACHE.update(at=time.time(), routes={})
    gateway = FakeGateway()
    monkeypatch.setattr(panel, "gateway_client", lambda: gateway)
    ids = add_sites(panel)
    page = admin.get("/proxy/hosts", headers={"HX-Request": ""}).text
    joined = ",".join(map(str, ids))
    assert f"/proxy/health?ids={joined}" in page and f"/analytics/sparks?ids={joined}" in page
    assert "hx-get=\"/analytics/spark?" not in page  # bez osobnego zapytania na wiersz

    health = admin.get(f"/proxy/health?ids={joined}").text
    assert [p[0] for p in gateway.posts] == ["/api/check/batch"]
    items = gateway.posts[0][1]["items"]
    assert items[0] == {"id": str(ids[0]), "url": "http://130.162.245.37:8080/", "host": "a.example.com"}
    assert health.count('hx-swap-oob="true"') == 3 and "200 · 12 ms" in health
    admin.get(f"/proxy/health?ids={joined}")
    assert len(gateway.posts) == 1  # wynik z pamięci (30 s)

    sparks = admin.get(f"/analytics/sparks?ids={joined}").text
    assert sparks.count('hx-swap-oob="true"') == 3 and f'id="spark-{ids[0]}"' in sparks and "<svg" in sparks
    assert gateway.gets == 1  # jedna paczka danych dla wszystkich wierszy


def test_health_falls_back_for_older_gateway(admin, monkeypatch):
    panel = admin.app.state.panel
    proxy_health._cache.clear()
    gateway = FakeGateway(batch=False)
    monkeypatch.setattr(panel, "gateway_client", lambda: gateway)
    ids = add_sites(panel, 2)
    page = admin.get(f"/proxy/health?ids={','.join(map(str, ids))}").text
    assert [p[0] for p in gateway.posts] == ["/api/check/batch", "/api/check", "/api/check"]
    assert page.count("nie odpowiada") == 2
