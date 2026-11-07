"""Kopie wolumenów: szyfrowanie w kawałkach (obcięcie i podmiana wychodzą), kopia z retencją, przywracanie."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, volume_backups


class Job:
    id = "j"

    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


def test_encryption_roundtrip_and_tamper_detection(panel, tmp_path):
    source = tmp_path / "a.tar"
    source.write_bytes(b"x" * (volume_backups.CHUNK * 2 + 123))  # trzy kawałki
    sealed = tmp_path / "a.dvb"
    volume_backups.encrypt_file(panel, source, sealed)
    data = sealed.read_bytes()
    assert b"xxxx" not in data
    out = tmp_path / "out.tar"
    assert volume_backups.decrypt_to(panel, data, out) == source.stat().st_size and out.read_bytes() == source.read_bytes()
    with pytest.raises(volume_backups.BackupError):
        volume_backups.decrypt_to(panel, data[: len(data) // 2], out)  # obcięty
    cut_last = data[: data.rfind(b"\x00\x10\x00")]  # sam początek bez ostatniego kawałka
    with pytest.raises(volume_backups.BackupError):
        volume_backups.decrypt_to(panel, cut_last, out)
    tampered = bytearray(data)
    tampered[-5] ^= 1
    with pytest.raises(volume_backups.BackupError):
        volume_backups.decrypt_to(panel, bytes(tampered), out)


class Node:
    def __init__(self, payload: bytes):
        self.payload, self.calls, self.uploaded = payload, [], None

    @contextlib.asynccontextmanager
    async def stream(self, method, path, **_):
        self.calls.append((method, path))

        class Response:
            async def aiter_bytes(inner, size):
                yield self.payload

        yield Response()

    async def delete(self, path, **_):
        self.calls.append(("DELETE", path))

    async def upload_file(self, path, file_path, **_):
        self.uploaded = Path(file_path).read_bytes()
        return {"transfer": "t-up"}


@pytest.fixture
def app_setup(admin, tmp_path, monkeypatch):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="n1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    spec = AppSpec.model_validate({"slug": "chmura", "source": {"kind": "image", "image": "nextcloud:29"}, "container_port": 80})
    app_id = deploy.create_app_row(panel, "Chmura", node_id, spec, {})
    panel.db.update("apps", app_id, status="running")
    panel.db.set_setting("backup_config", {"target": "local", "local_path": str(tmp_path / "store")})
    node = Node(b"TAR-PAYLOAD" * 1000)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    relayed = []

    async def follow(client, path, body, job, prefix=""):
        relayed.append((path, body))
        return {"result": {"transfer": "t1"}} if path.endswith("/export") else {"result": {}}

    monkeypatch.setattr(volume_backups.relay, "start_and_follow", follow)
    sid = volume_backups.save(panel, app_id, "0 3 * * *", 2, stop_app=True, enabled=True)
    return panel, app_id, sid, node, relayed, tmp_path


def test_backup_is_encrypted_pruned_and_restorable(app_setup):
    panel, app_id, sid, node, relayed, tmp_path = app_setup
    for _ in range(3):
        asyncio.run(volume_backups.run(panel, Job(), sid))
        import time

        time.sleep(1.1)  # różne znaczniki czasu w nazwach
    files = sorted((tmp_path / "store" / "volumes" / "chmura").iterdir())
    assert len(files) == 2  # retencja: 2 najnowsze
    assert b"TAR-PAYLOAD" not in files[-1].read_bytes()
    assert ("/api/apps/chmura/export", {"spec": relayed[0][1]["spec"], "stop": True}) == relayed[0]
    assert any(p.endswith("/power") and b["running"] is True for p, b in relayed)  # aplikacja wraca po kopii
    assert ("DELETE", "/api/transfers/t1") in node.calls
    row = panel.db.get("volume_backups", sid)
    assert row["last_status"] == "ok" and row["last_size"] == len(node.payload)

    relayed.clear()
    name = volume_backups.stored(panel, row)[0]["name"]
    asyncio.run(volume_backups.restore(panel, Job(), sid, name))
    assert node.uploaded == node.payload  # na węzeł trafia odszyfrowana paczka
    paths = [(p, b.get("running")) for p, b in relayed]
    assert paths == [("/api/apps/chmura/power", False), ("/api/apps/chmura/import", None), ("/api/apps/chmura/power", True)]


def test_card_and_validation(admin, app_setup):
    panel, app_id, sid, *_ = app_setup
    assert admin.post(f"/apps/{app_id}/volume-backups", data={"cron": "nie cron", "retention": "3"}).status_code == 400
    card = admin.get(f"/apps/{app_id}/volume-backups").text
    assert "0 3 * * *" in card
