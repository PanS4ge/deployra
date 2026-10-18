import pytest

from deployra_panel.services import external_wg, topology


def test_clean_defaults_and_validation():
    conf = external_wg.clean("10.8.0.1", "", "wg-easy", "")
    assert conf == {"address": "10.8.0.1", "subnet": "10.8.0.0/24", "container": "wg-easy", "proxy_address": "10.8.0.1"}
    assert external_wg.clean("10.8.0.7", "10.8.0.0/16", "", "172.17.0.1")["proxy_address"] == "172.17.0.1"
    for args in (("cezary", "", "", ""), ("10.8.0.1", "10.9.0.0/24", "", ""), ("10.8.0.1", "", "wg easy;rm", ""), ("10.8.0.1", "", "", "host")):
        with pytest.raises(ValueError):
            external_wg.clean(*args)


def test_compose_uses_wg_container_network_or_host_with_bind(panel):
    shared = panel.node(external_wg.create(panel, "cezary", "10.8.0.1", container="wg-easy"))
    host = panel.node(external_wg.create(panel, "pi", "10.8.0.9"))
    compose = external_wg.compose_snippet(shared)
    assert 'network_mode: "container:wg-easy"' in compose and "DEPLOYRA_API_BIND" not in compose and "ports:" not in compose
    compose = external_wg.compose_snippet(host)
    assert "network_mode: host" in compose and 'DEPLOYRA_API_BIND: "10.8.0.9"' in compose
    assert shared["api_url"] == "http://10.8.0.1:2750" and shared["transport"] == "direct" and shared["lan_subnets"] == ["10.8.0.0/24"]


def test_add_page_form_and_config_page(admin, panel):
    assert "/nodes/external-wg" in admin.get("/nodes/add").text
    response = admin.post("/nodes/external-wg", data={"name": "cezary", "address": "10.8.0.1", "container": "wg-easy"})
    assert response.status_code == 204
    node_id = int(response.headers["HX-Redirect"].split("/")[2])
    page = admin.get(f"/nodes/{node_id}/external-wg")
    assert page.status_code == 200 and "container:wg-easy" in page.text and f"/nodes/{node_id}/external-wg/pair" in page.text
    check = admin.get(f"/nodes/{node_id}/external-wg/check")  # nikt nie słucha pod 10.8.0.1 — panel nie widzi API
    assert check.status_code == 200 and "bi-x-circle-fill" in check.text
    bad = admin.post("/nodes/external-wg", data={"name": "x", "address": "nie-ip"})
    assert bad.status_code == 400
    plain = panel.db.insert("nodes", name="vps", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    assert admin.get(f"/nodes/{plain}/external-wg").status_code == 404


def test_map_hangs_external_node_on_its_gateway(panel):
    gw = panel.db.insert("gateway", name="oracle", role="proxy", status="paired", public_host="1.2.3.4", wg_enabled=0)
    node_id = external_wg.create(panel, "cezary", "10.8.0.1", gateway_id=gw)
    edges = {(link["source"], link["target"], link["kind"]) for link in topology.build(panel)["links"]}
    assert (f"gw-{gw}", f"node-{node_id}", "tunnel") in edges


WG_EASY = """[Interface]
PrivateKey = yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=
Address = 10.8.0.28/24
DNS = 1.1.1.1

[Peer]
PublicKey = xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg=
PresharedKey = Tq4lN6ZfS4cMv9mYxQ0oKZ3wP1aLb2Dg7XcVrSuE8hI=
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 0
Endpoint = vpn.example.com:51820
"""


def test_client_conf_limits_tunnel_to_wg_subnet(panel):
    node = panel.node(external_wg.create(panel, "promilek", "10.8.0.28"))
    wg0 = external_wg.client_conf(node, WG_EASY)
    assert "AllowedIPs = 10.8.0.0/24" in wg0 and "0.0.0.0/0" not in wg0  # reszta ruchu serwera nie idzie tunelem
    assert "DNS" not in wg0 and "PersistentKeepalive = 25" in wg0 and "PresharedKey = Tq4l" in wg0
    assert "Endpoint = vpn.example.com:51820" in wg0
    install = external_wg.install_snippet(wg0)
    assert "install -m 600 /dev/stdin /etc/wireguard/wg0.conf <<'EOF'" in install and "systemctl enable --now wg-quick@wg0" in install
    other = panel.node(external_wg.create(panel, "inny", "10.8.0.5"))
    with pytest.raises(ValueError):
        external_wg.client_conf(other, WG_EASY)


def test_client_conf_route_does_not_store_private_key(admin, panel):
    node_id = external_wg.create(panel, "promilek", "10.8.0.28")
    response = admin.post(f"/nodes/{node_id}/external-wg/client-conf", data={"conf": WG_EASY})
    assert response.status_code == 200 and "AllowedIPs = 10.8.0.0/24" in response.text
    assert "yAnz5TF" not in str(panel.node(node_id))
    assert admin.post(f"/nodes/{node_id}/external-wg/client-conf", data={"conf": "[Interface]\nfoo"}).status_code == 400


def test_check_uses_gateway_probe(panel, monkeypatch):
    import asyncio

    calls = []

    class FakeGateway:
        async def post(self, path, json):
            calls.append((path, json["url"]))
            return {"ok": True, "status": 200, "ms": 12}

        async def get(self, path):
            raise AssertionError("trasy tuneli sprawdzamy tylko, gdy test nie przeszedł")

    gw = panel.db.insert("gateway", name="cezary", role="proxy", status="paired", public_host="1.2.3.4", wg_enabled=0)
    node = panel.node(external_wg.create(panel, "promilek", "10.8.0.28", gateway_id=gw))
    monkeypatch.setattr(panel, "client_for_gateway", lambda _gw: FakeGateway())
    result = asyncio.run(external_wg.check(panel, node))
    assert result["gateway"]["ok"] is True and calls == [("/api/check", "http://10.8.0.28:2750/health")]


def test_host_mode_page_offers_wg0_conf_form(admin, panel):
    host = external_wg.create(panel, "promilek", "10.8.0.28")
    shared = external_wg.create(panel, "cezary", "10.8.0.1", container="wg-easy")
    assert f"/nodes/{host}/external-wg/client-conf" in admin.get(f"/nodes/{host}/external-wg").text
    assert "client-conf" not in admin.get(f"/nodes/{shared}/external-wg").text  # węzeł w sieci kontenera wg-easy nie potrzebuje klienta
