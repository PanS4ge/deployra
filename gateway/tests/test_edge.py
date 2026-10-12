import base64

import pytest
from fastapi.testclient import TestClient

from deployra_common import pairing, wg
from deployra_common.hmac_auth import sign
from deployra_gateway.app import create_api_app, create_bootstrap_app
from deployra_gateway.config import Config
from deployra_gateway.context import Context


class Reply:
    def __init__(self, response):
        self.status_code, self._json, self.text = response.status_code, response.json(), response.text

    def json(self):
        return self._json


@pytest.fixture
def edge(tmp_path, monkeypatch, ctx):
    """Hub = `ctx` (fixture z conftest), bramka brzegowa = osobny Context z DEPLOYRA_HUB."""
    token = pairing.JoinToken.generate()
    ctx.register_token(token.id, token.join_secret, "oracle", [], 60, kind="edge")
    for name in ("DATA", "NGINX_DIR", "CERTS_DIR", "ACME_DIR"):
        monkeypatch.setenv(f"DEPLOYRA_{name}", str(tmp_path / f"edge-{name.lower()}"))
    monkeypatch.setenv("DEPLOYRA_PUBLIC_HOST", "oracle.example.com")
    monkeypatch.setenv("DEPLOYRA_HUB", "mikrus.example.com:20123")
    monkeypatch.setenv("DEPLOYRA_JOIN", token.encode())
    edge_ctx = Context(Config())
    hub_bootstrap = TestClient(create_bootstrap_app(ctx))
    calls = []

    def post(url, json, timeout):
        calls.append(url)
        return Reply(hub_bootstrap.post("/join", json=json))

    edge_ctx.edge.join(post=post)
    return edge_ctx, token, calls


def test_edge_joins_hub_as_peer(ctx, edge):
    edge_ctx, token, calls = edge
    assert calls == ["http://mikrus.example.com:20123/join"]
    peer = ctx.state.get("peers")[token.id]
    assert peer["kind"] == "edge" and peer["address"] == edge_ctx.edge.address and peer["lan_subnets"] == []
    text = edge_ctx.edge.render()
    assert f"PublicKey = {ctx.hub.public_key}" in text and "AllowedIPs = 10.66.0.0/24" in text
    assert "Endpoint = vps.example.com:51820" in text and "PersistentKeepalive = 25" in text

    info = edge_ctx.public_info()
    assert info["role"] == "edge" and info["api_url"] == f"http://{edge_ctx.edge.address}:2751"
    assert edge_ctx.ensure_pairing_code() == token.pair_secret  # panel zna sekret z tokenu, hub nie


def test_panel_pairs_with_edge_using_token_secret(ctx, edge):
    edge_ctx, token, _ = edge
    client = TestClient(create_bootstrap_app(edge_ctx))
    priv, pub = pairing.new_keypair()
    _, wg_pub = wg.genkey()
    request = pairing.seal(token.pair_secret, {"kind": "pair-gateway", "x25519": pub, "nonce": "n1", "wg_public_key": wg.pubkey(wg_pub)})
    response = client.post("/pair", json=request)
    assert response.status_code == 200
    data = pairing.unseal(token.pair_secret, response.json(), "pair-gateway-ok")
    assert data["tunnel"] is None and data["gateway"]["role"] == "edge"  # tunel panel ma z hubem, nie z bramką brzegową
    assert edge_ctx.state.panel_secret and not edge_ctx.ensure_pairing_code()


def test_edge_routes_and_api(ctx, edge):
    edge_ctx, token, _ = edge
    edge_ctx.state.update(panel_secret=base64.b64encode(b"k" * 32).decode())
    api = TestClient(create_api_app(edge_ctx, start_scheduler=False))

    def call(method, path, json=None):
        import json as jsonlib

        body = jsonlib.dumps(json).encode() if json is not None else b""
        headers = sign(b"k" * 32, method, path, body)
        if json is not None:
            headers["Content-Type"] = "application/json"
        return api.request(method, path, content=body, headers=headers)

    response = call("PUT", "/api/routes", {"subnets": ["192.168.1.0/24", "192.168.1.7/24"]})
    assert response.json() == {"routes": ["192.168.1.0/24"], "edge": True}
    assert "AllowedIPs = 10.66.0.0/24, 192.168.1.0/24" in edge_ctx.edge.render()
    assert call("PUT", "/api/routes", {"subnets": ["10.66.0.0/25"]}).status_code == 400  # podsieć tunelu
    assert call("POST", "/api/join-tokens", {"id": "abc", "join_secret": "x" * 20}).status_code == 409  # to nie hub
    status = call("GET", "/api/status").json()
    assert status["role"] == "edge" and status["edge"]["routes"] == ["192.168.1.0/24"] and status["peers"] == []


def test_hub_rejects_unknown_token_kind(ctx):
    with pytest.raises(ValueError):
        ctx.register_token("abc123", "s" * 20, "x", [], 60, kind="admin")
