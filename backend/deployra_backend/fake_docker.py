"""Atrapa Dockera do testów i trybu deweloperskiego (`DEPLOYRA_FAKE_DOCKER=1`).

Udaje niskopoziomowe API docker-py w zakresie, którego używa backend: listy,
inspect, tworzenie/start/usuwanie kontenerów, logi (także strumień), statystyki.
Nie wykonuje niczego naprawdę — pozwala obejrzeć panel bez Dockera (np. na Windowsie).
"""

from __future__ import annotations

import hashlib
import itertools
import random
import threading
import time
from typing import Any


class NotFound(Exception):
    explanation = "nie znaleziono"


try:  # w obrazie jest prawdziwe docker-py — używamy jego wyjątków, żeby działały handlery API
    import docker.errors

    NotFound = docker.errors.NotFound  # type: ignore[misc]
except ImportError:  # pragma: no cover
    pass


class _LogStream:
    def __init__(self, name: str, tail: int, follow: bool) -> None:
        self.name, self.tail, self.follow = name, tail, follow
        self._closed = threading.Event()

    def __iter__(self):
        lines = [f"[{self.name}] start serwera…", f"[{self.name}] nasłuchuję na :80", f"[{self.name}] GET / 200 1.2ms"]
        for line in lines[-self.tail :] if self.tail else lines:
            yield (line + "\n").encode()
        while self.follow and not self._closed.wait(1.5):
            path = random.choice(["/", "/api/status", "/favicon.ico", "/login", "/assets/app.js"])
            code = random.choice([200, 200, 200, 304, 404])
            yield f"[{self.name}] GET {path} {code} {random.uniform(0.3, 40):.1f}ms\n".encode()

    def close(self) -> None:
        self._closed.set()


def _fake_tar(container: dict) -> None:
    """`tar czf /out/x.tar.gz …` w kontenerze pomocniczym → mały, poprawny plik w katalogu hosta zamontowanym jako /out."""
    command = container.get("command") or []
    if len(command) < 3 or command[0] != "tar" or "c" not in command[1]:
        return
    import io
    import tarfile
    from pathlib import Path

    for source, bind in (container["host_config"].get("binds") or {}).items():
        prefix = bind["bind"].rstrip("/") + "/"
        if command[2].startswith(prefix):
            with tarfile.open(Path(source) / command[2][len(prefix) :], "w:gz") as tar:
                data = f"atrapa wolumenu {list(container['host_config']['binds'])[0]}\n".encode()
                info = tarfile.TarInfo("README.txt")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


def _bytes(limit: Any) -> int:
    """"256m" → bajty (jak HostConfig.Memory w Dockerze; 0 = bez limitu)."""
    if not limit:
        return 0
    text = str(limit).lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    return int(float(text[:-1]) * units[text[-1]]) if text[-1] in units else int(text)


class FakeAPI:
    def __init__(self, demo: bool = False) -> None:
        self._ids = itertools.count(1)
        self.containers_: dict[str, dict] = {}
        self.images_ = [
            {"Id": "sha256:" + "a" * 64, "RepoTags": ["nginx:latest"], "Size": 192_000_000, "Created": 1_750_000_000},
            {"Id": "sha256:" + "b" * 64, "RepoTags": None, "Size": 10_000_000, "Created": 1_740_000_000},
        ]
        self.volumes_ = [{"Name": "dane", "Driver": "local", "Mountpoint": "/var/lib/docker/volumes/dane/_data", "CreatedAt": "2026-09-01T10:00:00Z"}]
        self.pulled: list[str] = []
        self.built: list[str] = []
        self.oneoff_runs: list[dict] = []
        self.on_oneoff: Any = None  # testy: podgląd/emulacja jednorazowych kontenerów (instalacja, tar wolumenów)
        self.crash_images: set[str] = set()
        self._cpu = 0
        if demo:
            for name, image, ports, labels in (
                ("app-blog", "ghost:5-alpine", {"2368/tcp": 20001}, {"deployra.app": "blog"}),
                ("nextcloud-app-1", "nextcloud:29-apache", {"80/tcp": 20002}, {"com.docker.compose.project": "nextcloud", "com.docker.compose.service": "app"}),
                ("nextcloud-db-1", "mariadb:11", {}, {"com.docker.compose.project": "nextcloud", "com.docker.compose.service": "db"}),
                ("grafana", "grafana/grafana:11.2.0", {"3000/tcp": 20003}, {}),
                ("deployra-static", "nginx:stable-alpine", {"80/tcp": 20080}, {"deployra.system": "static"}),
            ):  # fmt: skip
                cid = self.create_container(image, name=name, labels=labels, host_config={"port_bindings": ports})["Id"]
                self.start(cid)
            self.containers_[cid]["state"] = "running"
            old = self.create_container("redis:7-alpine", name="stary-redis", host_config={})["Id"]
            self.containers_[old]["state"] = "exited"

    # --- kontenery ---------------------------------------------------------------------

    def _find(self, cid: str) -> dict:
        for c in self.containers_.values():
            if c["id"] == cid or c["id"].startswith(cid) or c["name"] == cid:
                return c
        raise NotFound(f"brak kontenera {cid}")

    def containers(self, all: bool = False, filters: dict | None = None) -> list[dict]:
        out = []
        for c in self.containers_.values():
            if filters and "name" in filters and filters["name"] != f"^/{c['name']}$":
                continue
            out.append(
                {
                    "Id": c["id"], "Names": ["/" + c["name"]], "Image": c["image"], "ImageID": "sha256:" + "a" * 64,
                    "State": c["state"], "Status": "Up 3 hours" if c["state"] == "running" else "Exited (1) 2 days ago",
                    "Created": c["created"],
                    "Ports": [{"IP": "0.0.0.0", "PrivatePort": int(k.split("/")[0]), "PublicPort": v, "Type": "tcp"} for k, v in c["ports"].items()],
                    "Labels": c["labels"], "Mounts": [],
                    "NetworkSettings": {"Networks": {"deployra": {"NetworkID": "net1"}}},
                }
            )  # fmt: skip
        return out

    def create_host_config(self, **kwargs: Any) -> dict:
        return kwargs

    def create_container(self, image: str, command: Any = None, name: str | None = None, environment: Any = None,
                         labels: Any = None, ports: Any = None, host_config: Any = None, working_dir: str | None = None) -> dict:  # fmt: skip
        cid = hashlib.sha256(f"{next(self._ids)}-{name}-{time.time()}".encode()).hexdigest()  # losowe jak w Dockerze
        host_config = host_config or {}
        bindings = {k: (v[1] if isinstance(v, tuple) else v) for k, v in (host_config.get("port_bindings") or {}).items()}
        self.containers_[cid] = {
            "id": cid, "name": name or cid[:12], "image": image, "state": "created", "ports": bindings, "labels": labels or {},
            "env": environment, "host_config": host_config, "created": int(time.time()), "command": command, "working_dir": working_dir,
            "oom": False, "restarts": 0,
        }  # fmt: skip
        return {"Id": cid}

    def start(self, cid: str) -> None:
        c = self._find(cid)
        if c["labels"].get("deployra.system") in ("install", "transfer"):  # jednorazowe kontenery kończą się od razu
            c["state"] = "exited"
            self.oneoff_runs.append({"image": c["image"], "command": c["command"], "binds": (c["host_config"].get("binds") or {})})
            if self.on_oneoff:
                self.on_oneoff(c)
            elif c["labels"].get("deployra.system") == "transfer":
                _fake_tar(c)
            return
        c["state"] = "exited" if c["image"] in self.crash_images else "running"

    def wait(self, cid: str, timeout: Any = None) -> dict:
        c = self._find(cid)
        return {"StatusCode": 1 if c["image"] in self.crash_images else 0}

    def build(self, fileobj: Any = None, tag: str | None = None, **_: Any):
        self.built.append(tag or "")
        return iter([{"stream": "Step 1/2 : FROM base\n"}, {"stream": f"Successfully tagged {tag}\n"}])

    def stop(self, cid: str) -> None:
        self._find(cid)["state"] = "exited"

    def restart(self, cid: str) -> None:
        self.start(cid)

    kill = stop

    def pause(self, cid: str) -> None:
        self._find(cid)["state"] = "paused"

    def unpause(self, cid: str) -> None:
        self._find(cid)["state"] = "running"

    def inspect_container(self, cid: str) -> dict:
        c = self._find(cid)
        running = c["state"] == "running"
        return {
            "Id": c["id"], "Name": "/" + c["name"], "Created": "2026-10-10T08:00:00Z",
            "Config": {"Image": c["image"], "Labels": c["labels"], "Env": [f"{k}={v}" for k, v in (c["env"] or {}).items()] or ["PATH=/usr/bin"],
                       "Cmd": ["nginx", "-g", "daemon off;"], "Entrypoint": None},
            "RestartCount": c.get("restarts", 0),
            "State": {"Status": c["state"], "Running": running, "Restarting": False, "StartedAt": "2026-10-10T08:00:00Z",
                      "OOMKilled": c.get("oom", False), "FinishedAt": "2026-10-10T09:00:00Z" if c.get("oom") else "0001-01-01T00:00:00Z"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "Memory": _bytes(c["host_config"].get("mem_limit"))},
            "NetworkSettings": {"Networks": {"deployra": {"IPAddress": "172.30.0.5"}},
                                "Ports": {k: [{"HostIp": "0.0.0.0", "HostPort": str(v)}] for k, v in c["ports"].items()}},
            "Mounts": [{"Name": "dane", "Source": "/var/lib/docker/volumes/dane/_data", "Destination": "/data", "RW": True}],
        }  # fmt: skip

    def remove_container(self, cid: str, force: bool = False, v: bool = False) -> None:
        self.containers_.pop(self._find(cid)["id"])

    def logs(self, cid: str, tail: Any = None, stream: bool = False, follow: bool = False, timestamps: bool = False, **_: Any):
        c = self._find(cid)
        if stream:
            return _LogStream(c["name"], int(tail or 0), follow and c["state"] == "running")
        return b"blad: brak konfiguracji\n" if c["image"] in self.crash_images else f"[{c['name']}] działa\n".encode()

    def stats(self, cid: str, stream: bool = False, one_shot: bool | None = None, decode: Any = None) -> dict:
        self._cpu += random.randint(5_000_000, 60_000_000)
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": self._cpu}, "system_cpu_usage": int(time.time() * 1e9), "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": self._cpu - 20_000_000}, "system_cpu_usage": int(time.time() * 1e9) - 2_000_000_000},
            "memory_stats": {"usage": random.randint(40, 90) * 1024 * 1024, "limit": 1024 * 1024 * 1024, "stats": {"inactive_file": 1024 * 1024}},
            "networks": {"eth0": {"rx_bytes": 12_345_678, "tx_bytes": 2_345_678}},
            "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": 5_000_000}, {"op": "write", "value": 900_000}]},
            "pids_stats": {"current": 7},
        }  # fmt: skip

    def top(self, cid: str) -> dict:
        return {"Titles": ["PID", "CMD"], "Processes": [["1", "nginx: master"]]}

    def exec_create(self, cid: str, cmd: Any, tty: bool = False, **_: Any) -> dict:
        if tty:
            raise NotFound("terminal nie działa w trybie atrapy Dockera")
        self._find(cid)
        return {"Id": f"exec-{next(self._ids)}"}

    def exec_start(self, exec_id: str, **_: Any) -> bytes:
        return b"nginx: configuration file /etc/nginx/nginx.conf test is successful\n"

    def exec_inspect(self, exec_id: str) -> dict:
        return {"ExitCode": 0}

    # --- obrazy, sieci, wolumeny ----------------------------------------------------------

    def images(self, name: str | None = None) -> list[dict]:
        return list(self.images_)

    def inspect_image(self, image: str) -> dict:
        return {"Id": "sha256:" + "a" * 64}

    def pull(self, repo: str, tag: str | None = None, stream: bool = False, decode: bool = False, **_: Any):
        self.pulled.append(f"{repo}:{tag}")
        events = [{"status": f"Pulling from {repo}"}, {"status": "Pull complete", "id": "4f4fb700ef54"}, {"status": f"Downloaded newer image for {repo}:{tag}"}]
        return iter(events) if stream else events

    def remove_image(self, image: str, force: bool = False) -> None:
        self.images_ = [i for i in self.images_ if i["Id"] != image]

    def prune_images(self, filters: Any = None) -> dict:
        return {"SpaceReclaimed": 10_000_000}

    def networks(self, names: Any = None) -> list[dict]:
        nets = [
            {"Id": "net0", "Name": "bridge", "Driver": "bridge", "Scope": "local", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}},
            {"Id": "net1", "Name": "deployra", "Driver": "bridge", "Scope": "local", "IPAM": {"Config": [{"Subnet": "172.30.0.0/16"}]}},
        ]
        return [n for n in nets if not names or n["Name"] in names]

    def inspect_network(self, network_id: str) -> dict:
        return next(n for n in self.networks() if network_id in (n["Id"], n["Name"]))

    def create_network(self, *a: Any, **kw: Any) -> dict:
        return {"Id": "net2"}

    def remove_network(self, network_id: str) -> None:
        return None

    def prune_networks(self) -> dict:
        return {"NetworksDeleted": []}

    def volumes(self, filters: dict | None = None) -> dict:
        wanted = (filters or {}).get("label")
        if wanted:
            key, _, value = wanted.partition("=")
            return {"Volumes": [v for v in self.volumes_ if (v.get("Labels") or {}).get(key) == value]}
        return {"Volumes": list(self.volumes_)}

    def create_volume(self, name: str, labels: dict | None = None, **_: Any) -> dict:
        self.volumes_.append({"Name": name, "Driver": "local", "Mountpoint": f"/var/lib/docker/volumes/{name}/_data", "Labels": labels or {}})
        return {"Name": name}

    def inspect_volume(self, name: str) -> dict:
        for volume in self.volumes_:
            if volume["Name"] == name:
                return volume
        raise NotFound(f"brak wolumenu {name}")

    def remove_volume(self, name: str, force: bool = False) -> None:
        self.volumes_ = [v for v in self.volumes_ if v["Name"] != name]

    def prune_volumes(self) -> dict:
        return {"SpaceReclaimed": 0}

    def version(self) -> dict:
        return {"Version": "28.0.0 (atrapa)", "ApiVersion": "1.48", "Os": "linux", "Arch": "amd64", "KernelVersion": "6.8"}


class FakeDocker:
    def __init__(self, demo: bool = False) -> None:
        self.api = FakeAPI(demo)
