import subprocess
import time

from deployra_backend.config import Config
from deployra_backend.services.native_update import update_agent


def test_update_endpoint_with_sidecar(api, ctx, tmp_path):
    ctx.updates.dir = tmp_path / "upd"
    status = api.request("GET", "/api/update").json()
    assert status["available"] is False and status["service"] == "DeployraBackend"
    assert api.request("POST", "/api/update", json={"version": "1.2.0"}).status_code == 409

    (tmp_path / "upd").mkdir()
    (tmp_path / "upd" / "agent").write_text(f"mode=source\nupdated={int(time.time())}\n", encoding="utf-8")
    assert api.request("POST", "/api/update", json={"version": "zła"}).status_code == 400
    started = api.request("POST", "/api/update", json={"version": "v1.2.0"}).json()
    assert started["mode"] == "source" and started["state"] == "queued" and started["target"] == "1.2.0"
    assert api.request("POST", "/api/update", json={"version": "1.2.0"}).status_code == 409  # już trwa


def test_native_agent_uses_systemd_run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "install-native.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_NATIVE_REPO", str(repo))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))

    config = Config()
    assert config.update_dir == tmp_path / "data" / "update"
    agent = update_agent(config)
    assert agent.info()["mode"] == "native"
    agent.request("1.4.0")
    assert calls[0][0] == "systemd-run" and calls[0][-3:] == [str(repo / "deploy" / "install-native.sh"), "--update", "1.4.0"]
