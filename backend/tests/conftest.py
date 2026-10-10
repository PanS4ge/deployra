import base64
import time

import pytest
from fastapi.testclient import TestClient

from deployra_backend.app import create_app
from deployra_backend.config import Config
from deployra_backend.context import Context
from deployra_backend.fake_docker import FakeDocker
from deployra_common import pairing
from deployra_common.hmac_auth import sign


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_SRV", str(tmp_path / "srv"))
    monkeypatch.setenv("DEPLOYRA_NAME", "testowy")
    monkeypatch.setenv("DEPLOYRA_PORT_POOL", "41000-41050")
    fake = FakeDocker()
    context = Context(Config(), docker_factory=lambda: fake)
    context.wait_seconds = 0.2
    return context


def pair_panel(client: TestClient, ctx: Context) -> bytes:
    """Paruje „panel” z backendem kodem z logów i zwraca wspólny sekret."""
    from deployra_backend import enroll

    code = enroll.ensure_pairing_code(ctx)
    priv, pub = pairing.new_keypair()
    request = pairing.seal(code, {"kind": "pair-node", "x25519": pub, "nonce": "n1"})
    response = client.post("/pair", json=request)
    assert response.status_code == 200, response.text
    data = pairing.unseal(code, response.json(), "pair-node-ok")
    assert data["nonce"] == "n1"
    return pairing.derive_secret(priv, data["x25519"], code, "panel-node")


class Signed:
    def __init__(self, client: TestClient, secret: bytes):
        self.client, self.secret = client, secret

    def request(self, method, path, json=None):
        import json as jsonlib

        body = jsonlib.dumps(json).encode() if json is not None else b""
        headers = sign(self.secret, method, path, body)
        if json is not None:
            headers["Content-Type"] = "application/json"
        return self.client.request(method, path, content=body, headers=headers)

    def wait_job(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.request("GET", f"/api/jobs/{job_id}").json()
            if data["job"]["status"] in ("ok", "error"):
                return data
            time.sleep(0.05)
        raise AssertionError("zadanie nie skończyło się na czas")


@pytest.fixture
def api(ctx):
    app = create_app(ctx, start_services=False)
    with TestClient(app) as client:
        secret = pair_panel(client, ctx)
        assert base64.b64decode(ctx.state.get("api_secret")) == secret
        yield Signed(client, secret)
