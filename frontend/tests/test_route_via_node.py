"""Proxy przez węzeł: adres za węzłem (np. serwer WireGuard 10.8.0.x) — podsieć dopisywana sama, test trasy hop po hopie."""

from __future__ import annotations

from deployra_panel.db import now
from deployra_panel.services import sync


class Fake:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, json=None, **_):
        self.calls.append((method, path, json))
        return {"ok": True}

    async def put(self, path, json=None, **_):
        self.calls.append(("PUT", path, json))
        return {"ok": True, "applied": True, "status": "applied"}

    async def get(self, path, **_):
        self.calls.append(("GET", path))
        return {"ok": True, "peers": []}

    async def post(self, path, json=None, **_):
        self.calls.append(("POST", path, json))
        if path == "/api/net/probe":
            return {"ok": True, "ms": 2.5, "via": "wg0"}
        if path == "/api/check":
            return {"ok": True, "status": 200, "ms": 31}
        return {"ok": True}


def _setup(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.insert("gateway", name="hub", role="hub", status="paired", secret=panel.sealer.seal(b"h" * 32), api_url="http://10.66.0.1:2751",
                    subnet="10.66.0.0/24", hub_address="10.66.0.1")  # fmt: skip
    node_id = panel.db.insert("nodes", name="Cezary", transport="tunnel", tunnel_ip="10.66.0.6", peer_id="p6", status="paired", secret="x",
                              api_url="http://10.66.0.6:2750", lan_subnets=[])  # fmt: skip
    hub, node = Fake(), Fake()
    monkeypatch.setattr(panel, "_client", lambda key, url, secret: hub)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    return panel, node_id, hub, node


def test_host_through_node_adds_the_subnet_everywhere(admin, monkeypatch):
    panel, node_id, hub, node = _setup(admin, monkeypatch)
    saved = admin.post("/proxy/hosts/save", data={"kind": "proxy", "domains": "strych.example.com", "target_type": "address",
                                                  "host": "10.8.0.2", "port": "3923", "scheme": "http", "via_node": str(node_id)})  # fmt: skip
    assert saved.status_code == 204, saved.text
    assert panel.db.get("nodes", node_id)["lan_subnets"] == ["10.8.0.0/24"]
    assert ("PATCH", "/api/peers/p6", {"lan_subnets": ["10.8.0.0/24"]}) in hub.calls  # hub: AllowedIPs peera
    assert ("PUT", "/api/wg/lan", {"lan_subnets": ["10.8.0.0/24"]}) in node.calls  # węzeł: NAT na żywo
    assert sync.subnet_for(panel, panel.db.get("nodes", node_id), "10.8.0.9") is None  # już jest — drugi raz nie dopisuje
    assert sync.subnet_for(panel, panel.db.get("nodes", node_id), "10.66.0.4") is None  # sieć tunelu
    assert sync.subnet_for(panel, panel.db.get("nodes", node_id), "8.8.8.8") is None  # adres publiczny


def test_route_test_reports_each_hop(admin, monkeypatch):
    panel, node_id, hub, node = _setup(admin, monkeypatch)
    site_id = panel.db.insert("sites", kind="proxy", domains=["strych.example.com"], enabled=1, created_at=now(),
                              target={"type": "address", "host": "10.8.0.2", "port": 3923, "scheme": "http", "path": "", "via_node": node_id})  # fmt: skip
    page = admin.post(f"/proxy/hosts/{site_id}/route-test").text
    assert "Cezary" in page and "10.8.0.2:3923" in page and "wg0" in page and "HTTP 200" in page
    assert ("POST", "/api/net/probe", {"host": "10.8.0.2", "port": 3923}) in node.calls
