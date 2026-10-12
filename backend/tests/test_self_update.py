import hashlib
import itertools
import time
from types import SimpleNamespace

import pytest

from deployra_backend import __version__
from deployra_backend.config import Config
from deployra_backend.services import self_update
from deployra_backend.services.native_update import update_agent
from deployra_common.updates import read_kv

REPO = "ghcr.io/pans4ge/deployra-backend"
IMAGE_ENV = ["PATH=/usr/local/bin:/usr/bin", "PYTHONUNBUFFERED=1", "DEPLOYRA_DATA=/var/lib/deployra"]


class Engine:
    """Atrapa demona Dockera na potrzeby podmiany kontenera (inspect / pull / tag / create / rename…)."""

    def __init__(self, data_dir, crash=False, pull_error=None):
        self.data_dir, self.crash, self.pull_error = data_dir, crash, pull_error
        self.ids = itertools.count(1)
        self.images = {f"{REPO}:latest": "sha256:old"}
        self.image_config = {"sha256:old": self._image_cfg("1.1.3"), "sha256:new": self._image_cfg("9.9.9")}
        self.box = {}
        self.created = []
        old = self._add("deployra-backend", "sha256:old", {
            "Image": f"{REPO}:latest", "Hostname": "dom", "Env": IMAGE_ENV + ["DEPLOYRA_NAME=dom"], "Cmd": ["python", "-m", "deployra_backend"],
            "WorkingDir": "/app", "Labels": {"deployra.component": "backend", "org.opencontainers.image.version": "1.1.3",
                                             "com.docker.compose.project": "dom-stack", "deployra.system": "backend"}},
            {"NetworkMode": "host", "CapAdd": ["NET_ADMIN"], "Memory": 192 << 20, "RestartPolicy": {"Name": "unless-stopped"}})  # fmt: skip
        self.box[old]["Mounts"] = [
            {"Type": "bind", "Source": "/var/lib/deployra", "Destination": str(data_dir).replace("\\", "/"), "RW": True},
            {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
        ]
        self.old = old

    @staticmethod
    def _image_cfg(version):
        return {"Env": IMAGE_ENV, "Cmd": ["python", "-m", "deployra_backend"], "WorkingDir": "/app",
                "Labels": {"deployra.component": "backend", "org.opencontainers.image.version": version}}  # fmt: skip

    def _add(self, name, image, config, host, state="running"):
        cid = hashlib.sha256(f"{next(self.ids)}".encode()).hexdigest()
        self.box[cid] = {"Id": cid, "Name": "/" + name, "Image": image, "Config": config, "HostConfig": host, "RestartCount": 0,
                         "State": {"Status": state, "Running": state == "running", "ExitCode": 0},
                         "NetworkSettings": {"Networks": {"host": {}}}, "Mounts": []}  # fmt: skip
        return cid

    def _find(self, ref):
        for c in self.box.values():
            if ref in (c["Id"], c["Name"].lstrip("/")):
                return c
        raise LookupError(ref)

    # --- API ------------------------------------------------------------------------------
    def inspect_container(self, ref):
        return self._find(ref)

    def containers(self, all=False, filters=None):
        name = (filters or {}).get("name", "")[2:-1]
        return [{"Id": c["Id"], "State": c["State"]["Status"]} for c in self.box.values() if c["Name"].lstrip("/") == name]

    def inspect_image(self, ref):
        image_id = self.images.get(ref, ref)
        return {"Id": image_id, "Config": self.image_config[image_id]}

    def pull(self, repo, tag=None, stream=False, decode=False):
        if self.pull_error:
            return iter([{"status": "Pulling"}, {"error": self.pull_error}])
        self.images[f"{repo}:{tag}"] = "sha256:new"
        return iter([{"status": f"Pulling from {repo}"}, {"status": "Digest: sha256:abc"}, {"status": "Status: Downloaded newer image"}])

    def tag(self, image, repository, tag=None, force=False):
        self.images[f"{repository}:{tag}"] = self.images[image]

    def stop(self, cid, timeout=None):
        self._find(cid)["State"].update(Status="exited", Running=False)

    def rename(self, cid, name):
        if any(c["Name"] == "/" + name for c in self.box.values()):
            raise RuntimeError(f"Conflict: name {name} in use")
        self._find(cid)["Name"] = "/" + name

    def remove_container(self, cid, force=False):
        self.box.pop(self._find(cid)["Id"])

    def remove_image(self, image):
        if any(c["Image"] == image for c in self.box.values()):
            raise RuntimeError("image in use")

    def create_container_from_config(self, config, name=None):
        body = dict(config)
        host = body.pop("HostConfig")
        body.pop("NetworkingConfig", None)
        self.created.append(config)
        cid = self._add(name, self.images.get(config["Image"], config["Image"]), body, host, state="created")
        return {"Id": cid}

    def connect_container_to_network(self, *a, **kw):
        pass

    def start(self, cid):
        c = self._find(cid)
        if c["Image"] == "sha256:new" and self.crash:
            c["State"].update(Status="exited", Running=False, ExitCode=1)
            return
        c["State"].update(Status="running", Running=True)
        if c["Image"] == "sha256:new":  # nowy backend po starcie zgłasza wersję
            status = read_kv(self.data_dir / "update" / "status")
            (self.data_dir / "update" / "booted").write_text(f"version={status['version']}\nupdated={int(time.time())}\n", encoding="utf-8")

    def logs(self, cid, tail=None):
        return b"Traceback: boom\n"

    def create_host_config(self, **kw):
        return kw

    def create_container(self, image, command=None, name=None, environment=None, labels=None, host_config=None):
        cid = self._add(name, image, {"Cmd": command, "Env": environment, "Labels": labels}, host_config, state="created")
        return {"Id": cid}


def test_clone_keeps_user_settings_only(tmp_path):
    engine = Engine(tmp_path)
    info = engine.inspect_container("deployra-backend")
    body = self_update.clone(info, engine.image_config["sha256:old"], f"{REPO}:latest")
    assert body["Env"] == ["DEPLOYRA_NAME=dom"]  # zmienne obrazu daje nowy obraz
    assert "Cmd" not in body and "WorkingDir" not in body
    assert body["Labels"] == {"com.docker.compose.project": "dom-stack", "deployra.system": "backend"}  # stack Portainera zostaje
    assert body["HostConfig"]["NetworkMode"] == "host" and body["HostConfig"]["Memory"] == 192 << 20
    assert "NetworkingConfig" not in body and body["Hostname"] == "dom"


def test_apply_replaces_container(tmp_path):
    engine = Engine(tmp_path)
    assert self_update.apply(engine, tmp_path, engine.old, "9.9.9", poll=0.01)
    new = engine.inspect_container("deployra-backend")
    assert new["Id"] != engine.old and new["Image"] == "sha256:new" and new["State"]["Running"]
    assert new["Config"]["Image"] == f"{REPO}:latest" and engine.images[f"{REPO}:latest"] == "sha256:new"
    assert "DEPLOYRA_NAME=dom" in new["Config"]["Env"]
    assert list(engine.box) == [new["Id"]]  # stary kontener usunięty
    status = read_kv(tmp_path / "update" / "status")
    assert status["state"] == "done" and status["version"] == "9.9.9"
    assert "Updated to 9.9.9" in (tmp_path / "update" / "log").read_text(encoding="utf-8")


def test_apply_rolls_back_when_new_version_dies(tmp_path):
    engine = Engine(tmp_path, crash=True)
    assert not self_update.apply(engine, tmp_path, engine.old, "9.9.9", poll=0.01)
    back = engine.inspect_container("deployra-backend")
    assert back["Id"] == engine.old and back["State"]["Running"] and len(engine.box) == 1
    status = read_kv(tmp_path / "update" / "status")
    assert status["state"] == "error" and status["step"] == "rolled_back" and "exit code 1" in status["message"]
    assert "Traceback: boom" in (tmp_path / "update" / "log").read_text(encoding="utf-8")


def test_apply_pull_failure_leaves_container(tmp_path):
    engine = Engine(tmp_path, pull_error="unauthorized")
    assert not self_update.apply(engine, tmp_path, engine.old, "9.9.9")
    assert engine.inspect_container("deployra-backend")["State"]["Running"] and len(engine.box) == 1
    status = read_kv(tmp_path / "update" / "status")
    assert status["step"] == "pull_failed" and "unauthorized" in status["message"]


def test_backend_launches_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_UPDATE_DIR", str(tmp_path / "sidecar"))
    config = Config()
    engine = Engine(config.data_dir)
    monkeypatch.setenv("DEPLOYRA_CONTAINER", "deployra-backend")
    config = Config()
    agent = update_agent(config, lambda: SimpleNamespace(api=engine))
    assert agent.info()["mode"] == "image"
    info = agent.request("v9.9.9")
    assert info["state"] == "running" and info["step"] == "launching" and info["target"] == "9.9.9"
    helper = engine.inspect_container("deployra-backend-update")
    assert helper["Config"]["Cmd"] == ["python", "-m", "deployra_backend.services.self_update", engine.old, "9.9.9"]
    assert helper["Image"] == "sha256:old" and helper["HostConfig"]["auto_remove"] and helper["HostConfig"]["network_mode"] == "none"
    assert set(helper["HostConfig"]["binds"]) == {"/var/lib/deployra", "/var/run/docker.sock"}

    # pomocnik w toku → druga prośba odbita; nowy backend po starcie zgłasza wersję
    engine.start(helper["Id"])
    with pytest.raises(LookupError):
        agent.request("9.9.9")
    status = {"state": "running", "version": __version__}
    (config.data_dir / "update" / "status").write_text("".join(f"{k}={v}\n" for k, v in status.items()), encoding="utf-8")
    self_update.mark_booted(config)
    assert read_kv(config.data_dir / "update" / "booted")["version"] == __version__


def test_without_container_falls_back_to_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_UPDATE_DIR", str(tmp_path / "sidecar"))
    agent = update_agent(Config(), lambda: SimpleNamespace(api=Engine(tmp_path)))  # nie wiadomo, który kontener jest nasz
    assert agent.info()["available"] is False
    (tmp_path / "sidecar").mkdir()
    (tmp_path / "sidecar" / "agent").write_text(f"mode=source\nupdated={int(time.time())}\n", encoding="utf-8")
    assert agent.info()["mode"] == "source"
