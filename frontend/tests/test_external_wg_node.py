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
