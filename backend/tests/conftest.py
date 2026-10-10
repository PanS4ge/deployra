import base64
import itertools
import time

import pytest
from fastapi.testclient import TestClient

from deployra_backend.app import create_app
from deployra_backend.config import Config
from deployra_backend.context import Context
from deployra_common import pairing
from deployra_common.hmac_auth import sign


class FakeAPI:
    """Minimalna atrapa `docker.APIClient` na potrzeby testów."""

    def __init__(self):
        self._ids = itertools.count(1)
        self.containers_ = {}
        self.images_ = [
            {"Id": "sha256:" + "a" * 64, "RepoTags": ["nginx:latest"], "Size": 1000, "Created": 1},
            {"Id": "sha256:" + "b" * 64, "RepoTags": None, "Size": 10, "Created": 2},
        ]
        self.pulled = []
        self.crash_images = set()

    # kontenery
    def containers(self, all=False, filters=None):
        out = []
        for c in self.containers_.values():
            if filters and "name" in filters and filters["name"] != f"^/{c['name']}$":
                continue
            out.append(
                {
                    "Id": c["id"],
                    "Names": ["/" + c["name"]],
                    "Image": c["image"],
                    "ImageID": "sha256:" + "a" * 64,
                    "State": c["state"],
                    "Status": "Up 1 second" if c["state"] == "running" else "Exited (1)",
                    "Created": 0,
                    "Ports": [
                        {"IP": "0.0.0.0", "PrivatePort": int(k.split("/")[0]), "PublicPort": v, "Type": "tcp"}
                        for k, v in c["ports"].items()
                    ],
                    "Labels": c["labels"],
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"deployra": {"NetworkID": "net1"}}},
                }
            )
        return out

    def create_host_config(self, **kwargs):
        return kwargs

    def create_container(self, image, command=None, name=None, environment=None, labels=None, ports=None, host_config=None):
        cid = f"{next(self._ids):064x}"
        bindings = {k: (v[1] if isinstance(v, tuple) else v) for k, v in (host_config.get("port_bindings") or {}).items()}
        self.containers_[cid] = {
            "id": cid, "name": name, "image": image, "state": "created", "ports": bindings,
            "labels": labels or {}, "env": environment, "host_config": host_config,
        }  # fmt: skip
        return {"Id": cid}

    def start(self, cid):
        c = self.containers_[cid]
        c["state"] = "exited" if c["image"] in self.crash_images else "running"

    def inspect_container(self, cid):
        c = self.containers_.get(cid) or next((x for x in self.containers_.values() if x["name"] == cid), None)
        if c is None:
            import docker.errors

            raise docker.errors.NotFound("brak")
        return {
            "Id": c["id"],
            "Config": {"Image": c["image"], "Labels": c["labels"]},
            "State": {"Status": c["state"], "Running": c["state"] == "running", "Restarting": False},
        }

    def remove_container(self, cid, force=False, v=False):
        self.containers_.pop(cid)

    def logs(self, cid, tail=None, stream=False, **kw):
        return b"blad: brak konfiguracji\n"

    def stop(self, cid):
        self.containers_[cid]["state"] = "exited"

    restart = start

    # obrazy / sieci / wolumeny
    def images(self, name=None):
        return list(self.images_)

    def pull(self, repo, tag=None, stream=False, decode=False):
        self.pulled.append(f"{repo}:{tag}")
        return iter([{"status": "Pulling from library"}, {"status": "Pull complete", "id": "abc"}, {"status": "Downloaded"}])

    def networks(self, names=None):
        return [{"Id": "net1", "Name": "deployra", "Driver": "bridge", "Scope": "local", "IPAM": {"Config": []}}]

    def create_network(self, *a, **kw):
        return {"Id": "net2"}

    def volumes(self):
        return {"Volumes": [{"Name": "dane", "Driver": "local", "Mountpoint": "/x"}]}

    def version(self):
        return {"Version": "28.0.0", "ApiVersion": "1.48"}


class FakeDocker:
    def __init__(self):
        self.api = FakeAPI()


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
