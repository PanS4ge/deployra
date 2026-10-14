"""Testy serwera Model Context Protocol (MCP) i tokenów API."""

from __future__ import annotations

import json
import httpx
import pytest
from fastapi.testclient import TestClient

from deployra_panel import __version__
from deployra_panel.context import Panel
from deployra_panel.permissions import Permission
from deployra_panel.services import tokens
from deployra_panel.services.mcp_server import PROTOCOL_VERSION


def test_tokens_lifecycle(panel: Panel):
    # Utworzenie użytkownika testowego
    user_id = panel.db.insert("users", email="mcp@deployra.local", password_hash="hash", name="MCP Tester")

    # Tworzenie tokena
    raw_token, row = tokens.create_token(panel, "Cursor AI", user_id)
    assert raw_token.startswith("dpl_mcp_")
    assert row["name"] == "Cursor AI"
    assert row["prefix"].startswith("dpl_mcp_")

    # Weryfikacja poprawnego tokena
    auth_user = tokens.verify_token(panel, raw_token)
    assert auth_user is not None
    assert auth_user["email"] == "mcp@deployra.local"
    assert auth_user["token_id"] == row["id"]

    # Weryfikacja błędnego tokena
    assert tokens.verify_token(panel, "dpl_mcp_fake_token_1234567890") is None
    assert tokens.verify_token(panel, "") is None
    assert tokens.verify_token(panel, "invalid_prefix") is None

    # Lista tokenów
    tk_list = tokens.list_tokens(panel)
    assert len(tk_list) == 1
    assert tk_list[0]["name"] == "Cursor AI"

    # Usunięcie tokena
    assert tokens.delete_token(panel, row["id"]) is True
    assert tokens.verify_token(panel, raw_token) is None


def test_mcp_direct_jsonrpc_post(panel: Panel, client: TestClient):
    # Tworzymy użytkownika i token
    user_id = panel.db.insert("users", email="agent@deployra.local", password_hash="hash", name="Agent")
    raw_token, _ = tokens.create_token(panel, "Claude Desktop", user_id)

    # 1. Brak autoryzacji
    unauth = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert unauth.status_code == 401

    # 2. Błędny token
    bad_token = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer bad_token"})
    assert bad_token.status_code == 401

    headers = {"Authorization": f"Bearer {raw_token}"}

    # 3. Brak uprawnienia ALLOW_MCP -> 403 Forbidden
    forbidden = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
    assert forbidden.status_code == 403

    # Nadajemy uprawnienie ALLOW_MCP
    panel.db.update("users", user_id, permissions=Permission.ALLOW_MCP.value)

    # 4. Metoda initialize
    init_res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
        },
        headers=headers,
    )
    assert init_res.status_code == 200
    init_data = init_res.json()
    assert init_data["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert init_data["result"]["serverInfo"]["name"] == "deployra-mcp"
    assert init_data["result"]["serverInfo"]["version"] == __version__

    # 4. Metoda ping
    ping_res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers=headers)
    assert ping_res.status_code == 200
    assert ping_res.json()["result"] == {}

    # 5. Metoda tools/list
    tools_res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=headers)
    assert tools_res.status_code == 200
    tools_list = tools_res.json()["result"]["tools"]
    tool_names = {t["name"] for t in tools_list}
    assert "deployra_system_status" in tool_names
    assert "deployra_list_nodes" in tool_names
    assert "deployra_list_apps" in tool_names
    assert "deployra_list_containers" in tool_names
    assert "deployra_list_proxy_hosts" in tool_names

    # 6. tools/call: deployra_system_status
    call_res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "deployra_system_status", "arguments": {}}},
        headers=headers,
    )
    assert call_res.status_code == 200
    content = call_res.json()["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    status_obj = json.loads(content[0]["text"])
    assert status_obj["deployra_version"] == __version__
    assert "nodes" in status_obj
    assert "apps" in status_obj

    # 7. tools/call: nieznane narzędzie
    unknown_res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "non_existent_tool", "arguments": {}}},
        headers=headers,
    )
    assert unknown_res.status_code == 200
    assert unknown_res.json()["result"]["isError"] is True


def test_mcp_settings_page(panel: Panel, client: TestClient):
    # Logowanie jako admin
    admin_id = panel.db.insert("users", email="admin@deployra.local", password_hash="hash", name="Admin", permissions="0")
    from deployra_panel.security import SESSION_COOKIE

    sid, csrf = panel.sessions.create(admin_id, "127.0.0.1", "pytest")
    client.cookies.set(SESSION_COOKIE, sid)

    # Wejście na stronę /settings/mcp
    resp = client.get("/settings/mcp")
    assert resp.status_code == 200
    assert "Serwer MCP" in resp.text
    assert "Claude Desktop" in resp.text
    assert "Cursor" in resp.text

    # Tworzenie tokena przez formularz
    post_res = client.post("/settings/mcp/tokens", data={"name": "Testowy Agent", "csrf": csrf}, headers={"X-CSRF-Token": csrf}, follow_redirects=True)
    assert post_res.status_code == 200
    assert "dpl_mcp_" in post_res.text
    assert "Testowy Agent" in post_res.text


def test_mcp_query_token_auth_and_tools(panel: Panel, client: TestClient):
    user_id = panel.db.insert("users", email="tools@deployra.local", password_hash="hash", name="Tools Tester", permissions="0")
    raw_token, _ = tokens.create_token(panel, "Query Agent", user_id)

    # Wstawiamy testowe dane: node, app, site
    node_id = panel.db.insert("nodes", name="TestNode", transport="wireguard", status="paired")
    panel.db.insert("apps", name="Moja Apka", slug="moja-apka", node_id=node_id, status="running", domains=json.dumps(["apka.example.com"]), kind="docker", spec="{}")
    panel.db.insert("sites", kind="proxy", domains=["test.com"], target={"host": "10.99.0.2", "port": 80}, enabled=1)

    # 1. Autoryzacja przez query parameter ?token=
    ping_res = client.post(f"/mcp?token={raw_token}", json={"jsonrpc": "2.0", "id": 10, "method": "ping"})
    assert ping_res.status_code == 200

    # 2. Narzędzie deployra_list_nodes
    nodes_res = client.post(f"/mcp?token={raw_token}", json={"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "deployra_list_nodes", "arguments": {}}})
    assert nodes_res.status_code == 200
    nodes_data = json.loads(nodes_res.json()["result"]["content"][0]["text"])
    assert len(nodes_data) >= 1
    assert any(n["name"] == "TestNode" for n in nodes_data)

    # 3. Narzędzie deployra_list_apps
    apps_res = client.post(f"/mcp?token={raw_token}", json={"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "deployra_list_apps", "arguments": {}}})
    assert apps_res.status_code == 200
    apps_data = json.loads(apps_res.json()["result"]["content"][0]["text"])
    assert len(apps_data) >= 1
    assert any(a["slug"] == "moja-apka" for a in apps_data)

    # 4. Narzędzie deployra_list_proxy_hosts
    proxy_res = client.post(f"/mcp?token={raw_token}", json={"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "deployra_list_proxy_hosts", "arguments": {}}})
    assert proxy_res.status_code == 200
    proxy_data = json.loads(proxy_res.json()["result"]["content"][0]["text"])
    assert len(proxy_data) >= 1
    assert any("test.com" in p["domains"] for p in proxy_data)


@pytest.mark.anyio
async def test_mcp_sse_handshake(panel: Panel):
    from starlette.requests import Request
    from deployra_panel.routes.mcp import mcp_sse_connect, mcp_receive_message, _sse_sessions

    user_id = panel.db.insert("users", email="sse@deployra.local", password_hash="hash", name="SSE Tester", permissions="0")
    raw_token, _ = tokens.create_token(panel, "SSE Agent", user_id)

    # 1. Połączenie do SSE
    req = Request({
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "query_string": f"token={raw_token}".encode(),
        "headers": [],
    })
    resp = await mcp_sse_connect(req, panel=panel)
    assert resp.status_code == 200
    assert resp.media_type == "text/event-stream"

    gen = resp.body_iterator
    first = await anext(gen)
    assert "event: endpoint" in first
    assert "/mcp/messages?session_id=" in first

    # Pobieramy session_id
    import re
    m = re.search(r"session_id=([0-9a-f]+)", first)
    assert m is not None
    session_id = m.group(1)
    assert session_id in _sse_sessions

    # 2. Wysyłamy wiadomość JSON-RPC przez /mcp/messages
    import json
    post_req = Request({
        "type": "http",
        "method": "POST",
        "path": "/mcp/messages",
        "headers": [(b"content-type", b"application/json")],
    })
    post_req._json = {"jsonrpc": "2.0", "id": 99, "method": "ping"}
    post_resp = await mcp_receive_message(post_req, session_id=session_id, panel=panel)
    assert post_resp.status_code == 202

    # 3. Odbieramy odpowiedź z kolejki SSE
    second = await anext(gen)
    assert "event: message" in second
    assert '"id": 99' in second

    await gen.aclose()
    assert session_id not in _sse_sessions


def test_mcp_container_files_tools(panel: Panel, client: TestClient, monkeypatch):
    class MockNode:
        async def get(self, path, params=None, **_):
            if "/files/read" in path:
                return {"path": "/app/test.txt", "content": "print('hello')", "is_binary": False, "size": 14}
            if "/files" in path:
                return {"path": "/", "items": [{"name": "test.txt", "path": "/test.txt", "is_dir": False, "size": 14}]}
            return {"Id": "cid1", "Name": "/app"}

        async def post(self, path, json=None, **_):
            return {"ok": True}

    monkeypatch.setattr(panel, "node_client", lambda n: MockNode())

    user_id = panel.db.insert("users", email="mcpfiles@deployra.local", password_hash="hash", name="MCP Files Tester", permissions="0")
    node_id = panel.db.insert("nodes", name="TestNode", transport="wireguard", status="paired")
    raw_token, _ = tokens.create_token(panel, "MCP Files Agent", user_id)
    headers = {"Authorization": f"Bearer {raw_token}"}

    # 1. list_container_files
    call_list = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": {"name": "deployra_list_container_files", "arguments": {"node_id": node_id, "container_id": "cid1"}}},
        headers=headers,
    )
    assert call_list.status_code == 200
    res_data = json.loads(call_list.json()["result"]["content"][0]["text"])
    assert len(res_data["items"]) == 1
    assert res_data["items"][0]["name"] == "test.txt"

    # 2. read_container_file
    call_read = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 51, "method": "tools/call", "params": {"name": "deployra_read_container_file", "arguments": {"node_id": node_id, "container_id": "cid1", "path": "/app/test.txt"}}},
        headers=headers,
    )
    assert call_read.status_code == 200
    read_data = json.loads(call_read.json()["result"]["content"][0]["text"])
    assert read_data["content"] == "print('hello')"

    # 3. write_container_file
    call_write = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 52, "method": "tools/call", "params": {"name": "deployra_write_container_file", "arguments": {"node_id": node_id, "container_id": "cid1", "path": "/app/test.txt", "content": "updated content"}}},
        headers=headers,
    )
    assert call_write.status_code == 200
    write_data = json.loads(call_write.json()["result"]["content"][0]["text"])
    assert write_data["ok"] is True

