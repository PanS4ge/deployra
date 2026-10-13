from deployra_panel.services import proxy_health, proxy_state


class Gateway:
    def __init__(self, down):
        self.down = down

    async def post(self, path, json=None, **_):
        if path == "/api/check/batch":
            return {"results": {i["id"]: ({"status": None, "error": "timeout"} if i["url"].startswith(f"http://{self.down}") else
                                          {"status": 200, "ok": True, "ms": 5}) for i in json["items"]}}  # fmt: skip
        return {}

    async def get(self, path, **_):
        assert path == "/api/external-wg"
        return {"tunnels": [{"name": "wg1", "label": "Cezary", "routes": ["10.8.0.0/24"]}]}

    async def put(self, path, json=None, **_):
        return {"ok": True}


def test_passthrough_badge_hide_and_archive_unresponsive(admin, monkeypatch):
    panel = admin.app.state.panel
    proxy_health._cache.clear()
    proxy_state._EXTERNAL_CACHE.update(at=0.0, routes={})
    monkeypatch.setattr(panel, "gateway_client", lambda: Gateway("10.8.0.17"))
    monkeypatch.setattr(proxy_state, "push", lambda panel: _ok())
    alive = panel.db.insert("sites", kind="proxy", domains=["node.example.com"], target={"type": "address", "host": "10.8.0.11", "port": 80})
    dead = panel.db.insert("sites", kind="proxy", domains=["nodedell.example.com"], target={"type": "address", "host": "10.8.0.17", "port": 8080})
    public = panel.db.insert("sites", kind="proxy", domains=["x.example.com"], target={"type": "address", "host": "203.0.113.5", "port": 80})

    page = admin.get("/proxy/hosts", headers={"HX-Request": ""}).text
    assert page.count("przez Cezary") == 2 and "Ukryj nieodpowiadające" in page  # 10.8.0.x idzie przez bramkę Cezary
    assert "health-down" in admin.get("/proxy/health", params={"ids": f"{alive},{dead},{public}"}).text

    response = admin.post("/proxy/unresponsive/archive")
    assert response.status_code == 204
    assert panel.db.get("sites", dead)["archived"] == 1 and panel.db.get("sites", dead)["enabled"] == 0
    assert panel.db.get("sites", alive)["archived"] == 0
    page = admin.get("/proxy/hosts", headers={"HX-Request": ""}).text
    assert "nodedell.example.com" not in page and "Archiwum (1)" in page
    assert "nodedell.example.com" in admin.get("/proxy/hosts?archived=1", headers={"HX-Request": ""}).text

    admin.post(f"/proxy/hosts/{dead}/archive?restore=1")
    assert panel.db.get("sites", dead)["archived"] == 0 and panel.db.get("sites", dead)["enabled"] == 1
    proxy_health._cache.clear()
    admin.post("/proxy/unresponsive/delete")
    assert panel.db.get("sites", dead) is None and panel.db.get("sites", alive)


async def _ok():
    return {"ok": True, "status": "ok", "message": ""}
