import io
import zipfile

import pytest

from deployra_common.runtimes import RUNTIMES
from deployra_common.schemas import AppSpec


def deploy(api, spec):
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    return api.wait_job(job["id"])


def app_container(ctx, slug):
    return next(c for c in ctx.docker.api.containers_.values() if c["name"] == f"app-{slug}")


def test_runtime_defaults_port_and_rejects_unknown():
    spec = AppSpec.model_validate({"slug": "web", "source": {"kind": "runtime", "runtime": "python-flask"}})
    assert spec.container_port == 8000
    worker = AppSpec.model_validate({"slug": "bot", "source": {"kind": "runtime", "runtime": "python-script"}})
    assert worker.container_port is None
    with pytest.raises(ValueError):
        AppSpec.model_validate({"slug": "x", "source": {"kind": "runtime", "runtime": "cobol"}})
    with pytest.raises(ValueError):
        AppSpec.model_validate({"slug": "x", "endpoint": "ct-101", "source": {"kind": "runtime", "runtime": "php"}})


@pytest.mark.parametrize("runtime_id", sorted(RUNTIMES))
def test_every_runtime_is_complete(runtime_id):
    runtime = RUNTIMES[runtime_id]
    assert runtime.image and runtime.start and runtime.mount.startswith("/")
    assert runtime.starter or runtime.requires  # świeża aplikacja ma co uruchomić albo mówi, czego brakuje
    assert runtime.hint


def test_flask_app_gets_starter_install_and_mount(api, ctx):
    result = deploy(api, {"slug": "hello", "source": {"kind": "runtime", "runtime": "python-flask"}, "env": {"WORKERS": "1"}, "memory_mb": 192})
    assert result["job"]["status"] == "ok", result["lines"]
    root = ctx.config.sites_dir / "hello"
    assert (root / "app.py").is_file() and "flask" in (root / "requirements.txt").read_text()

    install = ctx.docker.api.oneoff_runs[0]
    assert install["image"] == "python:3.13-slim" and "pip install" in install["command"][-1]
    assert install["binds"][str(root)]["bind"] == "/app"

    container = app_container(ctx, "hello")
    assert container["command"][:2] == ["sh", "-c"] and "gunicorn" in container["command"][2]
    assert container["working_dir"] == "/app" and container["env"]["PORT"] == "8000" and container["env"]["WORKERS"] == "1"
    assert container["host_config"]["binds"][str(root)]["bind"] == "/app"
    assert container["ports"]["8000/tcp"] == ctx.state.get("apps")["hello"]["host_port"]
    assert not any(c["labels"].get("deployra.system") == "install" for c in ctx.docker.api.containers_.values())

    # drugi deploy nie nadpisuje plików użytkownika
    (root / "app.py").write_text("# moje\n", encoding="utf-8")
    assert deploy(api, {"slug": "hello", "source": {"kind": "runtime", "runtime": "python-flask"}})["job"]["status"] == "ok"
    assert (root / "app.py").read_text() == "# moje\n"


def test_worker_has_no_port_and_custom_command(api, ctx):
    spec = {"slug": "bot", "source": {"kind": "runtime", "runtime": "node-script", "start_command": "node bot.js"}}
    result = deploy(api, spec)
    assert result["job"]["status"] == "ok", result["lines"]
    container = app_container(ctx, "bot")
    assert container["ports"] == {} and container["command"] == ["sh", "-c", "node bot.js"]
    assert ctx.state.get("apps")["bot"]["host_port"] is None and "bot" not in (ctx.state.get("ports") or {})


def test_php_builds_base_image_once_and_needs_no_install(api, ctx):
    ctx.docker.api.inspect_image = lambda image: (_ for _ in ()).throw(KeyError(image)) if image.startswith("deployra/rt-") else {"Id": "x"}
    result = deploy(api, {"slug": "shop", "source": {"kind": "runtime", "runtime": "php"}})
    assert result["job"]["status"] == "ok", result["lines"]
    assert ctx.docker.api.built == [RUNTIMES["php"].run_image()]
    assert ctx.docker.api.oneoff_runs == []
    container = app_container(ctx, "shop")
    assert container["image"] == RUNTIMES["php"].run_image() and container["working_dir"] == "/var/www/html"
    assert (ctx.config.sites_dir / "shop" / "index.php").is_file()


def test_jar_without_files_fails_clearly(api, ctx):
    result = deploy(api, {"slug": "svc", "source": {"kind": "runtime", "runtime": "java"}})
    assert result["job"]["status"] == "error"
    assert any("*.jar" in line for line in result["lines"])


def test_failed_install_stops_deploy(api, ctx):
    ctx.docker.api.crash_images.add("node:22-alpine")
    result = deploy(api, {"slug": "web", "source": {"kind": "runtime", "runtime": "node-web"}})
    assert result["job"]["status"] == "error"
    assert not any(c["name"] == "app-web" for c in ctx.docker.api.containers_.values())


def test_zip_upload_keeps_data_and_cache(api, ctx):
    assert deploy(api, {"slug": "bot", "source": {"kind": "runtime", "runtime": "python-script"}})["job"]["status"] == "ok"
    root = ctx.config.sites_dir / "bot"
    (root / "data").mkdir()
    (root / "data" / "db.sqlite3").write_bytes(b"dane")
    (root / ".deployra" / "venv").mkdir(parents=True)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("bot/main.py", "print('v2')\n")
    body = buffer.getvalue()
    import hashlib

    from deployra_common.hmac_auth import sign

    headers = sign(api.secret, "POST", "/api/apps/bot/upload", b"", body_sha256=hashlib.sha256(body).hexdigest())
    response = api.client.post("/api/apps/bot/upload", content=body, headers={**headers, "Content-Type": "application/octet-stream"})
    assert response.status_code == 200, response.text
    assert (root / "main.py").read_text() == "print('v2')\n" and not (root / "requirements.txt").exists()
    assert (root / "data" / "db.sqlite3").read_bytes() == b"dane" and (root / ".deployra" / "venv").is_dir()


def test_remove_runtime_app_deletes_files(api, ctx):
    assert deploy(api, {"slug": "bot", "source": {"kind": "runtime", "runtime": "shell"}})["job"]["status"] == "ok"
    job = api.request("POST", "/api/apps/bot/remove", json={"delete_data": True}).json()["job"]
    assert api.wait_job(job["id"])["job"]["status"] == "ok"
    assert not (ctx.config.sites_dir / "bot").exists()
