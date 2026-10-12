import asyncio

from deployra_panel.context import LOCAL_UPSTREAM
from deployra_panel.services import local_node, sync


def test_panel_adopts_node_from_the_same_machine(admin, tmp_path, monkeypatch):
    panel = admin.app.state.panel
    code_file = tmp_path / "code"
    monkeypatch.setattr(panel.config, "local_node", "http://127.0.0.1:2750")
    monkeypatch.setattr(panel.config, "local_node_name", "dom")
    monkeypatch.setattr(panel.config, "local_node_code", code_file)
    calls = []

    async def fake_add(panel_, name, url, code, proxy_address):
        calls.append((name, url, code))
        node_id = panel.db.insert("nodes", name=name, transport="direct", api_url=url, status="paired", proxy_address="127.0.0.1")
        return panel.node(node_id)

    monkeypatch.setattr(sync, "add_direct_node", fake_add)
    assert asyncio.run(local_node.adopt(panel)) is None  # węzeł jeszcze nie wystawił kodu
    code_file.write_text("DPLR-ABCD-EFGH-IJKL\n", encoding="utf-8")
    node = asyncio.run(local_node.adopt(panel))
    assert calls == [("dom", "http://127.0.0.1:2750", "DPLR-ABCD-EFGH-IJKL")]
    assert node["proxy_address"] == LOCAL_UPSTREAM
    assert asyncio.run(local_node.adopt(panel))["id"] == node["id"] and len(calls) == 1  # już sparowany

    # bramka widzi aplikacje tego węzła przez tunel panelu; bez tunelu (panel przy bramce) — 127.0.0.1
    assert panel.node_upstream_host(node) == "127.0.0.1"
    panel.db.insert("gateway", name="Deployra-WG", panel_address="10.66.0.2/24")
    assert panel.node_upstream_host(node) == "10.66.0.2"


def test_no_local_node_configured(admin):
    assert asyncio.run(local_node.adopt(admin.app.state.panel)) is None
