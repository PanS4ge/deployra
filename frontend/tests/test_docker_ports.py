import pytest
from deployra_panel.routes.docker import extract_compose_host_ports


def test_extract_compose_host_ports():
    compose = """
services:
  web:
    image: nginx
    ports:
      - "8080:80"
      - "127.0.0.1:8443:443"
      - "53:53/udp"
      - 3000
  api:
    image: node
    ports:
      - target: 8000
        published: 9000
"""
    ports = extract_compose_host_ports(compose)
    assert 8080 in ports
    assert 8443 in ports
    assert 53 in ports
    assert 9000 in ports
    assert 3000 not in ports


class MockNodeClient:
    def __init__(self, ports):
        self.ports = ports

    async def get(self, path, **_):
        if "/api/ports" in path:
            return {"ports": self.ports}
        return []

    async def post(self, path, json=None, **_):
        return {"ok": True, "job": {"id": "j1"}}

    async def put(self, path, json=None, **_):
        return {"ok": True, "job": {"id": "j2"}}


def test_container_create_port_collision(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    mock_client = MockNodeClient([
        {"port": 8080, "protocol": "tcp", "owner": "existing-c1", "kind": "container"},
        {"port": 2222, "protocol": "tcp", "owner": "SFTP", "kind": "system"},
    ])
    monkeypatch.setattr(panel, "node_client", lambda n: mock_client)

    # Collision on 8080
    resp = admin.post(f"/nodes/{node_id}/containers/new", data={
        "name": "new-container",
        "image": "nginx:alpine",
        "ports": "8080:80",
    })
    assert resp.status_code == 409
    import json
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "Port hosta 8080 jest już zajęty przez existing-c1" in trigger["toast"]["message"]


def test_stack_save_port_collision(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node2", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    mock_client = MockNodeClient([
        {"port": 9000, "protocol": "tcp", "owner": "other-app", "kind": "container"},
    ])
    monkeypatch.setattr(panel, "node_client", lambda n: mock_client)

    # Collision on 9000
    compose = "services:\n  app:\n    image: redis\n    ports:\n      - '9000:6379'\n"
    resp = admin.post(f"/nodes/{node_id}/stacks/save", data={
        "name": "my-stack",
        "compose": compose,
    })
    assert resp.status_code == 409
    import json
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "Port hosta 9000 w compose jest już zajęty przez other-app" in trigger["toast"]["message"]
