import asyncio
import html
import json

import httpx
import pytest

from deployra_common import pairing
from deployra_panel.context import LOCAL_UPSTREAM
from deployra_panel.services import dns, sync


class FakeGateway:
    def __init__(self, name):
        self.name, self.calls, self.peers = name, [], []

    async def get(self, path, **_):
        self.calls.append(("GET", path))
        return {"peers": self.peers}

    async def post(self, path, json=None, **_):
        self.calls.append(("POST", path, json))
        return {"ok": True}

    async def put(self, path, json=None, **_):
        self.calls.append(("PUT", path, json))
        return {"ok": True, "status": "applied"}

    async def delete(self, path, **_):
        self.calls.append(("DELETE", path))
        return {"ok": True}

    async def request(self, method, path, json=None, **_):
        self.calls.append((method, path, json))
        return {"ok": True}


@pytest.fixture
def hub(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.insert("gateway", name="Deployra-WG", bootstrap_url="http://mikrus.example.com:20123", api_url="http://10.66.0.1:2751",
                    secret=panel.sealer.seal(b"h" * 32), public_host="mikrus.example.com", wg_port=20124, subnet="10.66.0.0/24",
                    hub_address="10.66.0.1", hub_public_key="k", panel_address=None)  # fmt: skip
    fakes = {"hub": FakeGateway("hub"), "edge": FakeGateway("edge")}
    monkeypatch.setattr(panel, "_client", lambda key, url, secret: fakes[key])
    return panel, fakes


def fake_edge(monkeypatch, address="10.66.0.7"):
    """Bramka brzegowa odpowiadająca na /pair (kod = sekret z tokenu)."""
    seen = []

    async def post_unsigned(url, body):
        seen.append(url)
        token_secret = fake_edge.secret
        request = pairing.unseal(token_secret, body, "pair-gateway")
        _, public = pairing.new_keypair()
        info = {"role": "edge", "public_host": "oracle.example.com", "api_url": f"http://{address}:2751", "tunnel_address": address,
                "subnet": "10.66.0.0/24", "hub_address": "10.66.0.1", "hub_public_key": "k", "wg_port": 20124}  # fmt: skip
        return pairing.seal(token_secret, {"kind": "pair-gateway-ok", "nonce": request["nonce"], "x25519": public, "tunnel": None,
                                           "gateway": info})  # fmt: skip

    monkeypatch.setattr(sync, "_post_unsigned", post_unsigned)
    return seen


def test_edge_invite_pair_switch_and_unlink(admin, hub, monkeypatch):
    panel, fakes = hub
    response = admin.post("/tunnel/edge", data={"public_host": "Oracle.example.com"})
    assert response.status_code == 200
    page = html.unescape(response.text)
    assert 'DEPLOYRA_HUB="mikrus.example.com:20123"' in page and 'DEPLOYRA_PUBLIC_HOST="oracle.example.com"' in page
    token = pairing.JoinToken.parse(page.split('DEPLOYRA_JOIN="')[1].split('"')[0])
    registered = next(c for c in fakes["hub"].calls if c[0] == "POST")
    assert registered[1] == "/api/join-tokens" and registered[2]["kind"] == "edge" and registered[2]["id"] == token.id
    row = panel.edge_row()
    assert row["status"] == "pending" and panel.edge()["role"] == "hub"  # do sparowania ruch zostaje na hubie

    # bramka brzegowa dołączyła do huba → odświeżenie tunelu paruje ją przez tunel sekretem z tokenu
    fake_edge.secret = token.pair_secret
    seen = fake_edge(monkeypatch)
    fakes["hub"].peers = [{"id": token.id, "kind": "edge", "name": "oracle", "address": "10.66.0.7", "lan_subnets": [], "endpoint": None,
                           "handshake_age": None, "latest_handshake": None, "online": False, "rx_bytes": 0, "tx_bytes": 0}]
    node_id = panel.db.insert("nodes", name="dom", transport="tunnel", status="paired", lan_subnets=["192.168.1.0/24"], tunnel_ip="10.66.0.3")
    assert asyncio.run(sync.refresh_pending(panel)) == 1
    assert seen == ["http://10.66.0.7:51821/pair"]
    edge = panel.edge_row()
    assert edge["status"] == "paired" and edge["api_url"] == "http://10.66.0.7:2751" and edge["panel_address"] == "10.66.0.7"
    assert panel.edge()["role"] == "edge"
    assert ("PUT", "/api/routes", {"subnets": ["192.168.1.0/24"]}) in fakes["edge"].calls
    assert any(c[:2] == ("PUT", "/api/proxy") for c in fakes["edge"].calls)
    hub_proxy = [c for c in fakes["hub"].calls if c[:2] == ("PUT", "/api/proxy")]
    assert hub_proxy and hub_proxy[-1][2]["sites"] == []  # hub nie trzyma już hostów

    # węzeł obok panelu, panel przy hubie → bramka brzegowa widzi go pod adresem huba w tunelu
    local = panel.node(panel.db.insert("nodes", name="vps", transport="direct", api_url="http://127.0.0.1:2750", status="paired",
                                       proxy_address=LOCAL_UPSTREAM))  # fmt: skip
    assert panel.node_upstream_host(local) == "10.66.0.1"
    assert panel.node_upstream_host(panel.node(node_id)) == "10.66.0.3"

    # tunel: status i tokeny idą do huba, nie do bramki brzegowej
    fakes["hub"].calls.clear()
    admin.get("/tunnel/status")
    assert ("GET", "/api/status") in fakes["hub"].calls and ("GET", "/api/status") not in fakes["edge"].calls

    assert admin.post("/tunnel/edge", data={"public_host": "inna.example.com"}).status_code == 409  # już jest

    assert admin.post("/tunnel/edge/unlink").status_code == 204
    assert panel.edge_row() is None and panel.edge()["role"] == "hub"
    assert ("DELETE", f"/api/peers/{token.id}") in fakes["hub"].calls
    assert panel.node_upstream_host(local) == "127.0.0.1"


def test_edge_validation(admin, hub):
    assert admin.post("/tunnel/edge", data={"public_host": "oracle.example.com:443"}).status_code == 400
    page = admin.get("/tunnel", headers={"HX-Request": ""}).text
    assert "edge-card" in page


def test_dns_points_at_edge(admin, hub):
    panel, _ = hub
    panel.db.insert("gateway", name="edge", role="edge", status="paired", secret=panel.sealer.seal(b"e" * 32),
                    public_host="203.0.113.9", api_url="http://10.66.0.7:2751")  # fmt: skip
    assert asyncio.run(dns.public_ip(panel)) == "203.0.113.9"
    assert json.dumps(panel.edge()["public_host"]) == '"203.0.113.9"'


def test_push_reports_the_gateway_that_failed(admin, hub):
    """Hub (pusty stan przy bramce brzegowej) przyjął, krawędź nie odpowiada — panel mówi, która bramka zawiodła."""
    from deployra_panel.services import proxy_state

    panel, fakes = hub
    panel.db.insert("gateway", name="oracle", role="edge", status="paired", secret=panel.sealer.seal(b"e" * 32),
                    public_host="203.0.113.9", api_url="http://10.66.0.7:2751")  # fmt: skip

    async def offline(path, json=None, **_):
        raise httpx.ConnectError("connection refused")

    fakes["edge"].put = offline
    result = asyncio.run(proxy_state.push(panel))
    assert result["status"] == "pending" and not result["ok"]
    assert "oracle: connection refused" in result["message"]
    assert {g["gateway"]: g["status"] for g in result["gateways"]} == {"Deployra-WG": "applied", "oracle": "unreachable"}

    async def rejected(path, json=None, **_):
        return {"ok": False, "status": "rejected", "message": "nginx: [emerg] unknown directive"}

    fakes["edge"].put = rejected
    result = asyncio.run(proxy_state.push(panel))
    assert result["status"] == "rejected" and result["message"].startswith("oracle: nginx")


def test_gateway_conf_preview(admin, hub):
    panel, fakes = hub
    site_id = panel.db.insert("sites", kind="proxy", domains=["a.example.com"], enabled=1, ssl_forced=0, http2=0, hsts=0, hsts_subdomains=0,
                              block_exploits=0, websockets=0, caching=0, trust_forwarded_proto=0)  # fmt: skip

    async def get(path, **_):
        fakes["hub"].calls.append(("GET", path))
        return {"conf": "server { listen 80; }", "last_apply": {"ok": True, "status": "applied"}}

    fakes["hub"].get = get
    page = admin.get(f"/proxy/hosts/{site_id}/gateway-conf", headers={"HX-Request": "true"}).text
    assert ("GET", f"/api/proxy/sites/{site_id}") in fakes["hub"].calls
    assert "server { listen 80; }" in page and "Deployra-WG" in page
