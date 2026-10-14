import pytest
from deployra_common.client import RemoteError
from deployra_panel.routes.docker import ep_param


def test_ep_param_handles_empty_or_none():
    assert ep_param("local") == "local"
    assert ep_param("ct-100") == "ct-100"
    assert ep_param("") == "local"
    assert ep_param("None") == "local"
    assert ep_param(None) == "local"


class MockNodeClient:
    def __init__(self, containers=None, should_fail_logs=False):
        self.containers = containers or []
        self.should_fail_logs = should_fail_logs

    async def get(self, path, **_):
        if "/api/containers/" in path and "/logs" not in path:
            cid = path.split("/api/containers/")[1].split("?")[0]
            c = next((item for item in self.containers if item.get("id") == cid or item.get("name") == cid or item.get("Id") == cid), None)
            if not c:
                raise RemoteError(404, f"No such container: {cid}")
            return c
        if "/api/containers" in path:
            return self.containers
        return []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def stream(self, method, path, **_):
        if self.should_fail_logs:
            raise RemoteError(404, "No such container")
        yield


def test_container_not_found_returns_404_not_502(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret=panel.sealer.seal(b"x" * 32))
    mock = MockNodeClient(containers=[])
    monkeypatch.setattr(panel, "node_client", lambda n: mock)

    resp = admin.get(f"/nodes/{node_id}/containers/nonexistent-container")
    # Must NOT be 502 Bad Gateway!
    assert resp.status_code == 404
    assert "502" not in resp.text
    assert "Nie znaleziono" in resp.text


def test_container_fuzzy_match_redirects(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret=panel.sealer.seal(b"x" * 32))
    real_container = {
        "id": "cid123456789",
        "Id": "cid123456789",
        "name": "myproject-web-1",
        "app": "myproject",
        "State": {"Status": "running", "Running": True},
        "Config": {"Image": "nginx:alpine", "Env": []},
        "NetworkSettings": {"Ports": {}},
    }
    mock = MockNodeClient(containers=[real_container])
    monkeypatch.setattr(panel, "node_client", lambda n: mock)

    resp = admin.get(f"/nodes/{node_id}/containers/app-myproject", follow_redirects=False)
    redirect_url = resp.headers.get("HX-Redirect") or resp.headers.get("location")
    assert resp.status_code in (204, 303)
    assert "cid123456789" in redirect_url


def test_logs_stream_handles_remote_error(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret=panel.sealer.seal(b"x" * 32))
    mock = MockNodeClient(containers=[{"id": "c1", "Id": "c1", "name": "app-test"}], should_fail_logs=True)
    monkeypatch.setattr(panel, "node_client", lambda n: mock)

    resp = admin.get(f"/nodes/{node_id}/containers/c1/logs/stream")
    # Must NOT be 502 Bad Gateway!
    assert resp.status_code == 200


def test_app_logs_and_files_redirect_to_running_container(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret=panel.sealer.seal(b"x" * 32))
    app_id = panel.db.insert("apps", name="MyApp", slug="myapp", node_id=node_id, kind="image", status="running", spec='{"source": {"kind": "image", "image": "nginx"}}')
    real_container = {
        "id": "cid-myapp-999",
        "Id": "cid-myapp-999",
        "name": "app-myapp",
        "app": "myapp",
    }
    mock = MockNodeClient(containers=[real_container])
    monkeypatch.setattr(panel, "node_client", lambda n: mock)

    resp_logs = admin.get(f"/apps/{app_id}/logs", follow_redirects=False)
    loc_logs = resp_logs.headers.get("HX-Redirect") or resp_logs.headers.get("location")
    assert resp_logs.status_code in (204, 303)
    assert "cid-myapp-999#logs" in loc_logs

    resp_files = admin.get(f"/apps/{app_id}/files", follow_redirects=False)
    loc_files = resp_files.headers.get("HX-Redirect") or resp_files.headers.get("location")
    assert resp_files.status_code in (204, 303)
    assert "cid-myapp-999/files" in loc_files


def test_app_logs_handles_missing_container_gracefully(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret=panel.sealer.seal(b"x" * 32))
    app_id = panel.db.insert("apps", name="EmptyApp", slug="emptyapp", node_id=node_id, kind="image", status="stopped", spec='{"source": {"kind": "image", "image": "nginx"}}')
    mock = MockNodeClient(containers=[])
    monkeypatch.setattr(panel, "node_client", lambda n: mock)

    # When no container exists, user is redirected back to app page with error message, NOT 502!
    resp = admin.get(f"/apps/{app_id}/logs", follow_redirects=False)
    assert resp.status_code in (204, 303)
    loc = resp.headers.get("HX-Redirect") or resp.headers.get("location")
    assert f"/apps/{app_id}" in loc
