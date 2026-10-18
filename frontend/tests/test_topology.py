from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, topology


def _app(panel, node_id, slug, endpoint="local"):
    spec = AppSpec.model_validate({"slug": slug, "node_id": node_id, "endpoint": endpoint, "source": {"kind": "image", "image": "nginx:alpine"},
                                   "container_port": 80})  # fmt: skip
    return deploy.create_app_row(panel, slug, node_id, spec, {})


def _edges(graph):
    return {(link["source"], link["target"], link["kind"]) for link in graph["links"]}


def test_graph_links_panel_gateways_nodes_ct_apps_and_domains(panel):
    hub = panel.db.insert("gateway", name="mikrus", role="hub", status="paired", public_host="srv1.mikr.us", wg_enabled=1)
    edge = panel.db.insert("gateway", name="oracle", role="edge", status="paired", public_host="1.2.3.4", secret="x")
    tunnel = panel.db.insert("nodes", name="dom", transport="tunnel", tunnel_ip="10.66.0.5", status="paired")
    direct = panel.db.insert("nodes", name="vps", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    web = _app(panel, tunnel, "web")
    cloud = _app(panel, direct, "cloud", endpoint="ct-101")
    panel.db.insert("sites", kind="proxy", domains=["web.example.com", "www.example.com"], target={"type": "node", "node_id": tunnel, "port": 20001},
                    app_id=web, enabled=1)  # fmt: skip
    panel.db.insert("sites", kind="proxy", domains=["nas.example.com"], target={"type": "address", "host": "10.8.0.12", "port": 443}, enabled=0)

    graph = topology.build(panel)
    by_id = {n["id"]: n for n in graph["nodes"]}
    edges = _edges(graph)

    assert ("panel", f"gw-{hub}", "manage") in edges
    assert (f"gw-{hub}", f"gw-{edge}", "tunnel") in edges  # brzegowa łączy się z hubem
    assert (f"gw-{hub}", f"node-{tunnel}", "tunnel") in edges
    assert ("panel", f"node-{direct}", "manage") in edges
    assert (f"node-{tunnel}", f"app-{web}", "host") in edges
    assert (f"node-{direct}", f"ct-{direct}-101", "host") in edges and (f"ct-{direct}-101", f"app-{cloud}", "host") in edges
    site_web = next(n for n in graph["nodes"] if n["type"] == "site" and n["label"].startswith("web."))
    assert site_web["label"] == "web.example.com +1"
    assert (f"app-{web}", site_web["id"], "traffic") in edges
    site_nas = next(n for n in graph["nodes"] if n["type"] == "site" and n["label"] == "nas.example.com")
    assert site_nas["status"] == "off" and (f"gw-{edge}", site_nas["id"], "traffic") in edges  # ręczny host przy bramce z ruchem
    assert by_id[f"app-{web}"]["url"] == f"/apps/{web}"
    ids = {link["source"] for link in graph["links"]} | {link["target"] for link in graph["links"]}
    assert ids <= set(by_id)


def test_map_page_and_data(admin):
    page = admin.get("/map")
    assert page.status_code == 200 and "topologyGraph" in page.text and 'href="/map"' in page.text
    data = admin.get("/map/data").json()
    assert data["nodes"][0]["id"] == "panel" and data["links"] == []
