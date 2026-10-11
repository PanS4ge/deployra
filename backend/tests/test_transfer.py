import hashlib
import tarfile

import pytest
from fastapi.testclient import TestClient

from conftest import Signed, pair_panel
from deployra_backend.app import create_app
from deployra_backend.config import Config
from deployra_backend.context import Context
from deployra_backend.fake_docker import FakeDocker
from deployra_backend.services import transfer
from deployra_common.hmac_auth import sign


@pytest.fixture
def second(tmp_path, monkeypatch):
    """Drugi węzeł — cel przenosin."""
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data2"))
    monkeypatch.setenv("DEPLOYRA_SRV", str(tmp_path / "srv2"))
    context = Context(Config(), docker_factory=lambda fake=FakeDocker(): fake)
    context.wait_seconds = 0.2
    with TestClient(create_app(context, start_services=False)) as client:
        yield Signed(client, pair_panel(client, context)), context


def run(api, method, path, json=None):
    response = api.request(method, path, json=json)
    assert response.status_code == 200, response.text
    return api.wait_job(response.json()["job"]["id"])


def move(api, ctx, target, target_ctx, slug, spec):
    """Eksport → pobranie → wysłanie → import, tak jak robi to panel."""
    exported = run(api, "POST", f"/api/apps/{slug}/export", {"spec": spec, "stop": True})
    assert exported["job"]["status"] == "ok", exported["lines"]
    transfer_id = exported["job"]["result"]["transfer"]
    body = api.request("GET", f"/api/transfers/{transfer_id}").content
    headers = sign(target.secret, "POST", "/api/transfers", b"", body_sha256=hashlib.sha256(body).hexdigest())
    uploaded = target.client.post("/api/transfers", content=body, headers={**headers, "Content-Type": "application/octet-stream"})
    assert uploaded.status_code == 200, uploaded.text
    api.request("DELETE", f"/api/transfers/{transfer_id}")
    assert not transfer.bundle_path(ctx.config, transfer_id).exists()
    imported = run(target, "POST", f"/api/apps/{slug}/import", {"transfer": uploaded.json()["transfer"]})
    return exported, imported


def test_move_runtime_app_with_files_data_and_sftp(api, ctx, second):
    target, target_ctx = second
    spec = {"slug": "bot", "source": {"kind": "runtime", "runtime": "python-script"}}
    assert run(api, "POST", "/api/apps/deploy", spec)["job"]["status"] == "ok"
    root = ctx.config.sites_dir / "bot"
    (root / "data").mkdir()
    (root / "data" / "state.json").write_text("{}", encoding="utf-8")
    (root / ".deployra" / "venv").mkdir(parents=True)
    (root / ".deployra" / "venv" / "big.bin").write_bytes(b"x" * 1000)
    api.request("PUT", "/api/sftp/bot-ftp", json={"slug": "bot", "password": "tajne-haslo-1"})

    exported, imported = move(api, ctx, target, target_ctx, "bot", spec)
    assert imported["job"]["status"] == "ok", imported["lines"]
    assert imported["job"]["result"]["sftp"] == ["bot-ftp"]
    moved = target_ctx.config.sites_dir / "bot"
    assert (moved / "main.py").is_file() and (moved / "data" / "state.json").read_text() == "{}"
    assert not (moved / ".deployra").exists()  # pamięć podręczna odbuduje się przy wdrożeniu
    assert target_ctx.state.get("sftp_users")["bot-ftp"]["hash"] == ctx.state.get("sftp_users")["bot-ftp"]["hash"]
    container = next(c for c in ctx.docker.api.containers_.values() if c["name"] == "app-bot")
    assert container["state"] == "exited"  # stara kopia zatrzymana na czas pakowania

    assert run(api, "POST", "/api/apps/bot/power", {"kind": "runtime", "running": True})["job"]["status"] == "ok"
    assert container["state"] == "running"
    assert run(target, "POST", "/api/apps/deploy", spec)["job"]["status"] == "ok"


def test_move_image_app_volumes_and_skips_host_paths(api, ctx, second):
    target, target_ctx = second
    spec = {"slug": "kuma", "source": {"kind": "image", "image": "louislam/uptime-kuma:1"}, "container_port": 3001,
            "volumes": [{"source": "kuma-data", "target": "/app/data"}, {"source": "/mnt/media", "target": "/media"}]}  # fmt: skip
    assert run(api, "POST", "/api/apps/deploy", spec)["job"]["status"] == "ok"
    exported, imported = move(api, ctx, target, target_ctx, "kuma", spec)
    assert exported["job"]["result"]["volumes"] == ["kuma-data"] and exported["job"]["result"]["skipped"] == ["/mnt/media"]
    assert any("/mnt/media" in line for line in exported["lines"])
    packing = [r for r in ctx.docker.api.oneoff_runs if r["image"] == transfer.HELPER_IMAGE]
    assert packing[0]["command"][:2] == ["tar", "czf"] and packing[0]["binds"]["kuma-data"]["mode"] == "ro"
    assert imported["job"]["status"] == "ok", imported["lines"]
    assert any(v["Name"] == "kuma-data" for v in target_ctx.docker.api.volumes_)
    restore = [r for r in target_ctx.docker.api.oneoff_runs if r["image"] == transfer.HELPER_IMAGE]
    assert "tar xzf /in/kuma-data.tar.gz" in restore[0]["command"][-1]


def test_failed_export_restarts_app(api, ctx):
    spec = {"slug": "web", "source": {"kind": "image", "image": "nginx"}, "container_port": 80, "volumes": [{"source": "web-data", "target": "/d"}]}
    assert run(api, "POST", "/api/apps/deploy", spec)["job"]["status"] == "ok"
    ctx.docker.api.crash_images.add(transfer.HELPER_IMAGE)
    result = run(api, "POST", "/api/apps/web/export", {"spec": spec, "stop": True})
    assert result["job"]["status"] == "error"
    container = next(c for c in ctx.docker.api.containers_.values() if c["name"] == "app-web")
    assert container["state"] == "running"
    assert not list(transfer.transfer_dir(ctx.config).glob("*.tar"))


def test_import_rejects_foreign_bundle_and_bad_id(api, ctx, tmp_path):
    work = tmp_path / "evil"
    work.mkdir()
    (work / "manifest.json").write_text('{"slug": "other", "files": false}', encoding="utf-8")
    bundle = transfer.transfer_dir(ctx.config) / "0123456789abcdef.tar"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "w") as tar:
        tar.add(work, arcname=".")
    result = run(api, "POST", "/api/apps/mine/import", {"transfer": "0123456789abcdef"})
    assert result["job"]["status"] == "error" and not bundle.exists()
    assert api.request("POST", "/api/apps/mine/import", json={"transfer": "../../etc"}).status_code == 400
    assert api.request("GET", "/api/transfers/nope").status_code == 400


def test_usage_reports_app_containers(api, ctx):
    spec = {"slug": "web", "source": {"kind": "image", "image": "nginx"}, "container_port": 80, "memory_mb": 128}
    assert run(api, "POST", "/api/apps/deploy", spec)["job"]["status"] == "ok"
    first = api.request("GET", "/api/usage").json()
    app = first["apps"]["web"]
    assert app["limit"] == 128 * 1024 * 1024 and app["running"] == 1 and app["mem"] > 0 and app["oom_at"] is None
    assert "ram" in first["host"]
    container = next(c for c in ctx.docker.api.containers_.values() if c["name"] == "app-web")
    container.update(oom=True, restarts=4)
    second = api.request("GET", "/api/usage").json()["apps"]["web"]
    assert second["oom_at"] and second["restarts"] == 4 and second["cpu"] is not None
