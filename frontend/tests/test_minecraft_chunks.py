import base64
import hashlib
import io
import time

import pytest

from deployra_panel.routes import minecraft as mc_routes
from deployra_panel.services import minecraft as mc


class FakePlugin:
    """Plugin 1.2 w pamięci: te same zasady kawałków co ServerFiles (kolejność, rozmiar, SHA-256)."""

    def __init__(self, chunked=True):
        self.files, self.parts, self.ops = {}, {}, []
        self.chunked = chunked
        self.fail_at = None

    async def call(self, _panel, _row, op, args=None, timeout=20):
        args = args or {}
        self.ops.append(op)
        if op == "files.write":
            self.files[args["path"]] = base64.b64decode(args["data_b64"])
            return {}
        if op == "files.list":
            prefix = args["path"].rstrip("/") + "/"
            names = {p[len(prefix):].split("/")[0] for p in self.files if p.startswith(prefix)}
            return {"items": [{"name": n, "is_dir": False, "size": 1} for n in sorted(names)]}
        if not self.chunked:
            raise mc.BridgeError("IllegalArgumentException: unknown operation: " + op)
        key = (args["path"], args.get("upload"))
        if op == "files.write_chunk":
            if self.fail_at is not None and args["offset"] >= self.fail_at:
                raise mc.BridgeError("AccessDeniedException: " + args["path"])
            data = base64.b64decode(args["data_b64"])
            current = b"" if args["offset"] == 0 else self.parts.get(key, b"")
            assert len(current) == args["offset"], "chunk out of order"
            self.parts[key] = current + data
            return {"size": len(self.parts[key])}
        if op == "files.commit":
            data = self.parts.pop(key)
            assert len(data) == args["size"] and hashlib.sha256(data).hexdigest() == args["sha256"]
            self.files[args["path"]] = data
            return {}
        if op == "files.abort":
            self.parts.pop(key, None)
            return {}
        if op == "files.read_chunk":
            data = self.files[args["path"]]
            piece = data[args["offset"]:args["offset"] + args["length"]]
            return {"data_b64": base64.b64encode(piece).decode(), "size": len(data)}
        raise AssertionError(op)


@pytest.fixture
def plugin(panel, monkeypatch):
    fake = FakePlugin()
    monkeypatch.setattr(mc, "call", fake.call)
    monkeypatch.setattr(mc, "online", lambda _row: True)
    monkeypatch.setattr(mc, "CHUNK", 1024)
    monkeypatch.setattr(mc, "FILE_LIMIT", 4096)
    monkeypatch.setattr(mc_routes, "SYNC_UPLOAD", 8192)
    return fake


def _server(panel, features=("files.chunks",), version="1.2.0"):
    server_id, _ = mc.create(panel, "Kolega")
    panel.db.update("minecraft_servers", server_id, secret="x", info={"plugin_version": version, "features": list(features)})
    return server_id


def _wait(panel, job_id):
    for _ in range(200):
        job = panel.jobs.get(job_id)
        if job and job.done:
            return job
        time.sleep(0.02)
    raise AssertionError("zadanie nie skończyło się")


def test_big_folder_goes_in_chunks_as_background_job(panel, admin, plugin):
    server_id = _server(panel)
    big = bytes(range(256)) * 20  # 5120 B > FILE_LIMIT → kawałki po 1024
    files = [("files", ("plugins/BlueMap.jar", io.BytesIO(big), "application/java-archive")),
             ("files", ("plugins/BlueMap/core.conf", io.BytesIO(b"x: 1"), "text/plain"))]
    response = admin.post(f"/minecraft/{server_id}/files/upload", data={"dir_path": "/"}, files=files)
    assert response.status_code == 204 and response.headers["HX-Redirect"].startswith("/jobs/")
    job = _wait(panel, response.headers["HX-Redirect"].split("/")[2])
    assert job.status == "ok", job.lines
    assert plugin.files["/plugins/BlueMap.jar"] == big and plugin.files["/plugins/BlueMap/core.conf"] == b"x: 1"
    # jar: 5 pełnych kawałków + pusty zamykający, core.conf: jeden; po wszystkim nie zostaje żaden plik tymczasowy
    assert plugin.ops.count("files.write_chunk") == 7 and plugin.parts == {}


def test_failed_chunk_aborts_and_reports_readable_error(panel, admin, plugin):
    server_id = _server(panel)
    plugin.fail_at = 2048
    response = admin.post(f"/minecraft/{server_id}/files/upload", data={"dir_path": "/plugins"},
                          files=[("files", ("Big.jar", io.BytesIO(b"z" * 5000), "application/java-archive"))])  # fmt: skip
    job = _wait(panel, response.headers["HX-Redirect"].split("/")[2])
    assert job.status == "error" and "Brak uprawnień" in job.error and "/plugins/Big.jar" in job.error
    assert "files.abort" in plugin.ops and plugin.parts == {} and "/plugins/Big.jar" not in plugin.files


def test_small_files_still_go_immediately(panel, admin, plugin):
    server_id = _server(panel)
    response = admin.post(f"/minecraft/{server_id}/files/upload", data={"dir_path": "/plugins"},
                          files=[("files", ("small.yml", io.BytesIO(b"a: 1"), "text/yaml"))])  # fmt: skip
    assert response.status_code == 204 and "tab=files" in response.headers["HX-Redirect"]
    assert plugin.files["/plugins/small.yml"] == b"a: 1" and "files.write_chunk" not in plugin.ops


def test_big_file_needs_updated_plugin(panel, admin, plugin):
    server_id = _server(panel, features=(), version="1.1.0")
    response = admin.post(f"/minecraft/{server_id}/files/upload", data={"dir_path": "/"},
                          files=[("files", ("world.zip", io.BytesIO(b"w" * 5000), "application/zip"))])  # fmt: skip
    assert response.status_code == 413 and "1.2" in response.headers["HX-Trigger"]


def test_download_streams_big_file_in_chunks(panel, admin, plugin):
    server_id = _server(panel)
    blob = b"0123456789" * 500
    plugin.files["/world/level.dat"] = blob
    response = admin.get(f"/minecraft/{server_id}/files/download", params={"path": "/world/level.dat"})
    assert response.status_code == 200 and response.content == blob and response.headers["content-length"] == str(len(blob))
    assert plugin.ops.count("files.read_chunk") == 5
    missing = admin.get(f"/minecraft/{server_id}/files/download", params={"path": "/nope.dat"})
    assert missing.status_code != 200


def test_plugin_update_goes_to_update_folder(panel, admin, plugin):
    server_id = _server(panel, features=(), version="1.1.0")
    plugin.files["/plugins/DeployraBridge-1.1.0.jar"] = b"old"
    assert mc.plugin_outdated(panel.db.get("minecraft_servers", server_id))
    page = admin.get(f"/minecraft/{server_id}?tab=files", headers={"HX-Request": ""}).text
    assert f"/minecraft/{server_id}/plugin/update" in page
    response = admin.post(f"/minecraft/{server_id}/plugin/update")
    assert response.status_code == 200 and "plugins/update" in response.headers["HX-Trigger"]
    assert plugin.files["/plugins/update/DeployraBridge-1.1.0.jar"] == mc.JAR.read_bytes()


def test_plugins_tab_lists_and_toggles_plugins(panel, admin, monkeypatch):
    import json

    server_id = _server(panel, features=("files.chunks", "plugins.manage"))
    monkeypatch.setattr(mc, "online", lambda _row: True)
    sent = []

    async def fake_call(_panel, _row, op, args=None, timeout=20):
        sent.append((op, args))
        if op == "plugins.list":
            return {"plugins": [
                {"name": "DeployraBridge", "version": "1.2.0", "enabled": True, "file": "DeployraBridge.jar", "persisted": False, "self": True},
                {"name": "Essentials", "version": "2.21", "enabled": True, "file": "EssentialsX.jar", "persisted": False, "self": False},
                {"name": "Old", "enabled": False, "file": "Old.jar", "persisted": True, "loaded": False},
            ]}
        return {"restart": op == "plugins.enable" and not args.get("name")}

    monkeypatch.setattr(mc, "call", fake_call)
    page = admin.get(f"/minecraft/{server_id}/plugins").text
    assert "Essentials" in page and "łączy z panelem" in page and "wyłączony na stałe" in page
    assert page.count('"name": "DeployraBridge"') == 0  # przy własnym pluginie nie ma przycisków
    response = admin.post(f"/minecraft/{server_id}/plugins/action", data={"name": "Essentials", "action": "disable_persist"})
    assert response.status_code == 200 and sent[-1] == ("plugins.disable", {"name": "Essentials", "file": "", "persist": True})
    assert json.loads(response.headers["HX-Trigger"])["mc-plugins"] is True
    response = admin.post(f"/minecraft/{server_id}/plugins/action", data={"name": "", "file": "Old.jar", "action": "enable"})
    assert "po restarcie" in json.loads(response.headers["HX-Trigger"])["toast"]["message"]
    assert admin.post(f"/minecraft/{server_id}/plugins/action", data={"name": "x", "action": "rm -rf"}).status_code == 400


def test_plugins_tab_asks_old_plugin_to_update(panel, admin):
    server_id = _server(panel, features=(), version="1.1.0")
    assert "DeployraBridge 1.2" in admin.get(f"/minecraft/{server_id}/plugins").text
