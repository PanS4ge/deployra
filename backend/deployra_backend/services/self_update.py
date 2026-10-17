"""Backend aktualizuje się sam — bez osobnego kontenera aktualizatora.

Na prośbę panelu backend uruchamia krótko żyjącą kopię siebie (ten sam obraz, AutoRemove), która:

    1. pobiera nowy obraz (`…/deployra-backend:<wersja>`),
    2. zatrzymuje stary kontener i zmienia mu nazwę na `<nazwa>-old`,
    3. tworzy nowy kontener z tą samą konfiguracją (zmienne, wolumeny, sieć, etykiety
       compose / Portainera) i nowym obrazem,
    4. czeka, aż nowy backend wstanie i zgłosi swoją wersję w pliku `booted`,
    5. sukces → usuwa stary kontener; błąd → przywraca stary,
    6. kończy się, a Docker usuwa ją razem z nią (AutoRemove).

Nie potrzebuje plików compose, więc działa tak samo dla stacka z Portainera, `docker compose`
i `docker run`. Instalacja ze źródeł (lokalny obraz `deployra-backend`, bez rejestru) idzie
skryptem aktualizatora (git + build przez `docker compose`), wbudowanym w obraz backendu.

Stan i log: katalog `update/` w danych backendu (protokół z deployra_common.updates) —
musi być na wolumenie, żeby nowy kontener widział, co zrobił stary. Log jest techniczny,
po angielsku, jak w skrypcie aktualizatora.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deployra_common.i18n import tr
from deployra_common.updates import append_log, normalize, read_kv, same_version, write_kv, write_status

from .. import __version__

MOUNTINFO = re.compile(r"/containers/([0-9a-f]{64})/")
SCRIPT = "/usr/local/bin/deployra-updater.sh"
SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
PEERS = ("panel", "gateway")  # komponenty bez docker.sock — podmienia je backend z tego samego hosta
CACHE_SECONDS = 60
VERIFY_SECONDS = 180
# wartości, które kontener dziedziczy po obrazie — nowy obraz ma dać swoje
INHERITED = ("Cmd", "Entrypoint", "WorkingDir", "User", "ExposedPorts", "Volumes", "Healthcheck", "StopSignal", "Shell", "OnBuild")


def container_id(config: Any, proc: Path = Path("/proc")) -> str | None:
    """ID własnego kontenera: DEPLOYRA_CONTAINER albo ścieżki /var/lib/docker/containers/<id>/ w mountinfo."""
    if config.container:
        return config.container
    try:
        found = MOUNTINFO.search((proc / "self" / "mountinfo").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return found.group(1) if found else None


def split_ref(ref: str) -> tuple[str, str | None]:
    """`ghcr.io/a/b:1.2` → (`ghcr.io/a/b`, `1.2`); bez taga → (repo, None)."""
    ref = ref.split("@", 1)[0]
    name, sep, tag = ref.rpartition(":")
    if sep and "/" not in tag:
        return name, tag
    return ref, None


class SelfUpdater:
    def __init__(self, config: Any, docker: Callable[[], Any]) -> None:
        self.config = config
        self.docker = docker
        self.dir = config.data_dir / "update"
        self._cache: tuple[float, dict | None] = (-CACHE_SECONDS, None)
        self.reason: str | None = None  # dlaczego aktualizator jest niedostępny (pokazywane w panelu)

    def me(self, fresh: bool = False) -> dict | None:
        """`docker inspect` własnego kontenera albo None (nie w Dockerze / brak dostępu do docker.sock)."""
        at, info = self._cache
        if not fresh and time.monotonic() - at < CACHE_SECONDS:
            return info
        info, reason = None, None
        cid = container_id(self.config) if self.config.self_update else None
        if not self.config.self_update:
            reason = "disabled"
        elif not cid:
            reason = "no_container"
        else:
            try:
                found = self.docker().api.inspect_container(cid)
                if ((found.get("Config") or {}).get("Labels") or {}).get("deployra.component") == "backend":
                    info = found
                else:
                    reason = "not_backend"
            except Exception:  # noqa: BLE001 — brak Dockera = brak samodzielnej aktualizacji
                reason = "no_docker"
        self._cache = (time.monotonic(), info)
        self.reason = reason
        return info

    def mode(self) -> str | None:
        info = self.me()
        if not info:
            return None
        repo, _ = split_ref(info["Config"]["Image"])
        if "/" in repo:  # ghcr.io/…/deployra-backend → nowy obraz z rejestru
            return "image"
        labels = info["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project.working_dir") and Path(SCRIPT).is_file():
            return "source"  # lokalny build z repozytorium → git + build
        self.reason = "local_image"
        return None

    def _helper_setup(self, info: dict) -> tuple[dict, dict, str]:
        """Wolumen danych, socket Dockera i sieć dla kontenera pomocniczego (z obrazu backendu)."""
        mounts = {m.get("Destination"): m for m in info.get("Mounts") or []}
        data = mounts.get(str(self.config.data_dir).replace("\\", "/"))
        if not data:
            raise LookupError(tr("srv.upd.no_data_mount", path=self.config.data_dir))
        binds = {data.get("Name") or data["Source"]: {"bind": data["Destination"], "mode": "rw"}}
        env = {"DEPLOYRA_DATA": data["Destination"], "DEPLOYRA_LANG": os.environ.get("DEPLOYRA_LANG", "")}
        network = "none"  # pobiera demon Dockera — pomocnik nie potrzebuje sieci
        sock = next((mounts[s] for s in SOCKETS if s in mounts), None)
        if sock:
            binds[sock["Source"]] = {"bind": sock["Destination"], "mode": "rw"}
        if os.environ.get("DOCKER_HOST"):  # np. docker-socket-proxy po TCP
            env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
            network = (info.get("HostConfig") or {}).get("NetworkMode") or "default"
        return binds, env, network

    def _clear_helper(self, api: Any, helper: str, version: str) -> None:
        for old in api.containers(all=True, filters={"name": f"^/{helper}$"}):
            if old.get("State") == "running":
                raise LookupError(tr("srv.upd.running", version=version))
            api.remove_container(old["Id"], force=True)

    # --- panel i bramka na tym samym hoście Dockera (bez własnego aktualizatora) ---

    def peers(self) -> list[dict]:
        """Kontenery panelu i bramki, które ten backend może podmienić na nową wersję."""
        if not self.me():
            return []
        try:
            found = self.docker().api.containers(filters={"label": "deployra.component"})
        except Exception:  # noqa: BLE001
            return []
        out = []
        for c in found:
            component = (c.get("Labels") or {}).get("deployra.component")
            if component not in PEERS:
                continue
            name = ((c.get("Names") or ["/"])[0]).lstrip("/")
            status = read_kv(self.dir / "peers" / name / "status")
            out.append({"id": c["Id"], "name": name, "component": component, "image": c.get("Image"), "state": status.get("state", "idle"),
                        "target": status.get("version"), "step": status.get("step"), "message": status.get("message", ""),
                        "updated": status.get("updated")})  # fmt: skip
        return out

    def peer_log(self, name: str, lines: int = 30) -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            return []
        try:
            return (self.dir / "peers" / name / "log").read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return []

    def launch_peer(self, container: str, version: str) -> dict:
        version = normalize(version)
        info = self.me(fresh=True)
        if not info:
            raise LookupError(tr("srv.upd.no_updater"))
        api = self.docker().api
        try:
            target = api.inspect_container(container)
        except Exception as exc:  # noqa: BLE001
            raise LookupError(tr("srv.upd.no_peer", name=container)) from exc
        component = ((target.get("Config") or {}).get("Labels") or {}).get("deployra.component")
        if component not in PEERS:
            raise LookupError(tr("srv.upd.no_peer", name=container))
        repo, _ = split_ref(target["Config"]["Image"])
        if "/" not in repo:
            raise LookupError(tr("srv.upd.local_image"))
        name = target["Name"].lstrip("/")
        helper = f"{name}-update"
        self._clear_helper(api, helper, version)
        binds, env, network = self._helper_setup(info)
        directory = self.dir / "peers" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "log").write_text("", encoding="utf-8")
        append_log(directory, f"Update of {name} ({component}) to {version} requested — starting {helper}")
        write_status(directory, "running", version, "launching")
        host_config = api.create_host_config(binds=binds, network_mode=network, auto_remove=True)
        command = ["python", "-m", "deployra_backend.services.self_update", target["Id"], version, component]
        created = api.create_container(info["Image"], command=command, name=helper, environment=env,
                                       labels={"deployra.system": "updater"}, host_config=host_config)  # fmt: skip
        api.start(created["Id"])
        return {"name": name, "component": component, "target": version}

    def launch(self, version: str) -> None:
        info = self.me(fresh=True)
        if not info:
            raise LookupError(tr("srv.upd.no_updater"))
        api = self.docker().api
        name = info["Name"].lstrip("/")
        helper = f"{name}-update"
        self._clear_helper(api, helper, version)
        binds, env, network = self._helper_setup(info)

        if self.mode() == "source":
            labels = info["Config"]["Labels"]
            workdir = labels["com.docker.compose.project.working_dir"]
            repo = posixpath.dirname(workdir)  # konteksty budowania to .. względem deploy/
            binds[repo] = {"bind": repo, "mode": "rw"}
            env.update(DEPLOYRA_UPDATE_DIR=posixpath.join(env["DEPLOYRA_DATA"], "update"), DEPLOYRA_IMAGES="deployra",
                       DEPLOYRA_UPDATE_SERVICES=labels.get("com.docker.compose.service", ""), UPD_PROJECT=labels.get("com.docker.compose.project", ""),
                       UPD_WORKDIR=workdir, UPD_FILES=labels.get("com.docker.compose.project.config_files", ""),
                       UPD_ENVFILES=labels.get("com.docker.compose.project.environment_file", ""), UPD_IMAGE=info["Image"])  # fmt: skip
            command = ["sh", SCRIPT, "apply", version]
        else:
            command = ["python", "-m", "deployra_backend.services.self_update", info["Id"], version]

        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "log").write_text("", encoding="utf-8")
        append_log(self.dir, f"Update to {version} requested — starting {helper}")
        write_status(self.dir, "running", version, "launching")
        host_config = api.create_host_config(binds=binds, network_mode=network, auto_remove=True)
        created = api.create_container(info["Image"], command=command, name=helper, environment=env,
                                       labels={"deployra.system": "updater"}, host_config=host_config)  # fmt: skip
        api.start(created["Id"])


def mark_booted(config: Any) -> None:
    """Nowy backend po starcie: „wstałem w wersji X” — pomocnik czeka na ten znak."""
    directory = config.data_dir / "update"
    status = read_kv(directory / "status")
    if status.get("state") in ("queued", "running") and same_version(status.get("version"), __version__):
        write_kv(directory / "booted", {"version": __version__, "updated": int(time.time())})


# --- pomocnik (osobny kontener) -----------------------------------------------------------


def clone(info: dict, image_config: dict | None, image: str) -> dict:
    """Konfiguracja nowego kontenera: to, co ustawił użytkownik (compose, Portainer, docker run), bez wartości ze starego obrazu."""
    config = dict(info.get("Config") or {})
    base = image_config or {}
    for key in INHERITED:
        if key in config and config[key] == base.get(key):
            del config[key]
    image_env = set(base.get("Env") or [])
    config["Env"] = [e for e in config.get("Env") or [] if e not in image_env]
    image_labels = base.get("Labels") or {}
    config["Labels"] = {k: v for k, v in (config.get("Labels") or {}).items() if image_labels.get(k) != v}
    config["Image"] = image
    config.pop("MacAddress", None)
    host = dict(info.get("HostConfig") or {})
    mode = host.get("NetworkMode") or "default"
    if mode.startswith("container:"):
        config.pop("Hostname", None)
        config.pop("Domainname", None)
    elif config.get("Hostname") == info["Id"][:12]:  # domyślny hostname = skrócone ID starego kontenera
        config.pop("Hostname", None)
    config["HostConfig"] = host
    primary = _primary_network(info)
    if primary:
        endpoint = ((info.get("NetworkSettings") or {}).get("Networks") or {})[primary]
        config["NetworkingConfig"] = {"EndpointsConfig": {primary: _endpoint(endpoint, info["Id"])}}
    return config


def _primary_network(info: dict) -> str | None:
    mode = (info.get("HostConfig") or {}).get("NetworkMode") or "default"
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    if mode in ("host", "none") or mode.startswith("container:") or not networks:
        return None
    return mode if mode in networks else next(iter(networks))


def _endpoint(network: dict, cid: str) -> dict:
    out: dict[str, Any] = {}
    aliases = [a for a in network.get("Aliases") or [] if a != cid[:12]]
    if aliases:
        out["Aliases"] = aliases
    for key in ("IPAMConfig", "Links", "DriverOpts"):
        if network.get(key):
            out[key] = network[key]
    return out


def apply(api: Any, data_dir: Path, cid: str, version: str, wait: float = VERIFY_SECONDS, poll: float = 2.0, component: str = "backend",
          settle: float = 20.0) -> bool:  # fmt: skip
    """Podmienia kontener na nową wersję. `component` = backend (sam siebie) albo panel/bramka z tego hosta."""
    directory = Path(data_dir) / "update"
    if component != "backend":  # stan sąsiada osobno — nie miesza się z aktualizacją samego backendu
        directory = directory / "peers" / api.inspect_container(cid)["Name"].lstrip("/")
    directory.mkdir(parents=True, exist_ok=True)
    version = normalize(version)

    def log(line: str) -> None:
        append_log(directory, line)
        print(line, flush=True)

    def status(state: str, step: str, message: str = "") -> None:
        write_status(directory, state, version, step, message)

    info = api.inspect_container(cid)
    name = info["Name"].lstrip("/")
    ref = info["Config"]["Image"]
    repo, tag = split_ref(ref)
    log(f"Updating {name} to {version} — pulling {repo}:{version}")
    status("running", "pulling")
    try:
        for event in api.pull(repo, tag=version, stream=True, decode=True):
            if event.get("error"):
                raise RuntimeError(event["error"])
            text = str(event.get("status") or "")
            if text.startswith(("Digest:", "Status:")):
                log(text)
    except Exception as exc:  # noqa: BLE001
        log(f"✖ pull failed: {exc}")
        status("error", "pull_failed", str(exc)[:300])
        return False
    final = f"{repo}:{version}"
    if tag in (None, "latest"):  # stack z `:latest` zostaje przy `:latest` — przestawiamy tag na nowy obraz
        final = ref
        api.tag(f"{repo}:{version}", repo, tag or "latest", force=True)
    new_image = api.inspect_image(final)
    if new_image["Id"] == info["Image"]:
        log("The image didn't change — this version is already running")
        status("done", "done")
        return True

    body = clone(info, (api.inspect_image(info["Image"]) or {}).get("Config"), final)
    backup = f"{name}-old"
    for old in api.containers(all=True, filters={"name": f"^/{backup}$"}):
        api.remove_container(old["Id"], force=True)  # zostałość po wcześniejszej aktualizacji
    status("running", "starting")
    started = int(time.time())
    try:
        api.stop(cid, timeout=30)
        api.rename(cid, backup)
    except Exception as exc:  # noqa: BLE001
        log(f"✖ couldn't stop the old container: {exc}")
        _restore(api, None, cid, name, log)
        status("error", "recreate_failed", str(exc)[:300])
        return False

    new_id = None
    try:
        new_id = api.create_container_from_config(body, name=name)["Id"]
        primary = _primary_network(info)
        for net, endpoint in ((info.get("NetworkSettings") or {}).get("Networks") or {}).items():
            if primary and net != primary:
                api.connect_container_to_network(new_id, net, aliases=_endpoint(endpoint, info["Id"]).get("Aliases"))
        api.start(new_id)
        log(f"Started the new container ({new_id[:12]}), waiting for it to report in")
        status("running", "verifying")
        if component == "backend":
            problem = _wait_booted(api, new_id, directory, version, started, wait, poll)
        else:
            problem = _wait_healthy(api, new_id, wait, poll, settle)
    except Exception as exc:  # noqa: BLE001
        problem = str(exc) or type(exc).__name__
    if problem:
        log(f"✖ the new version didn't come up: {problem} — rolling back")
        if new_id:
            try:
                for line in api.logs(new_id, tail=20).decode("utf-8", "replace").splitlines():
                    log(f"    {line}")
            except Exception:  # noqa: BLE001
                pass
        _restore(api, new_id, cid, name, log)
        status("error", "rolled_back", problem[:300])
        return False

    try:
        api.remove_container(cid, force=True)
    except Exception as exc:  # noqa: BLE001
        log(f"(couldn't remove {backup}: {exc})")
    try:
        api.remove_image(info["Image"], force=False)  # stary obraz tylko zajmuje dysk; w użyciu → zostaje
    except Exception:  # noqa: BLE001
        pass
    try:
        for img in api.images(filters={"label": f"deployra.component={component}"}):
            iid = img.get("Id")
            if iid and iid != new_image.get("Id"):
                try:
                    api.remove_image(iid, force=False)
                except Exception:
                    pass
    except Exception:  # noqa: BLE001
        pass
    log(f"✔ Updated to {version}")
    status("done", "done")
    return True


def _wait_booted(api: Any, cid: str, directory: Path, version: str, since: int, wait: float, poll: float) -> str | None:
    deadline = time.monotonic() + wait
    while True:
        info = api.inspect_container(cid)
        state = info.get("State") or {}
        if state.get("Status") in ("exited", "dead") or (info.get("RestartCount") or 0) > 0:
            return f"container {state.get('Status')} (exit code {state.get('ExitCode')})"
        booted = read_kv(directory / "booted")
        if same_version(booted.get("version"), version) and int(booted.get("updated") or 0) >= since - 1:
            return None
        if time.monotonic() > deadline:
            return f"no sign of life within {int(wait)} s"
        time.sleep(poll)


def _wait_healthy(api: Any, cid: str, wait: float, poll: float, settle: float) -> str | None:
    """Panel i bramka nie piszą znaku życia do naszego katalogu: liczy się healthcheck obrazu albo stabilna praca."""
    deadline = time.monotonic() + wait
    started = time.monotonic()
    while True:
        info = api.inspect_container(cid)
        state = info.get("State") or {}
        if state.get("Status") in ("exited", "dead") or (info.get("RestartCount") or 0) > 0:
            return f"container {state.get('Status')} (exit code {state.get('ExitCode')})"
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy":
            return None
        if health == "unhealthy":
            return "healthcheck: unhealthy"
        if health is None and time.monotonic() - started >= settle:
            return None
        if time.monotonic() > deadline:
            return f"no sign of life within {int(wait)} s"
        time.sleep(poll)


def _restore(api: Any, new_id: str | None, old_id: str, name: str, log: Callable[[str], None]) -> None:
    try:
        if new_id:
            api.remove_container(new_id, force=True)
        if api.inspect_container(old_id)["Name"].lstrip("/") != name:
            api.rename(old_id, name)
    except Exception as exc:  # noqa: BLE001
        log(f"(rename back failed: {exc})")
    try:
        api.start(old_id)
        log("Previous version is running again")
    except Exception as exc:  # noqa: BLE001
        log(f"✖ couldn't start the previous version: {exc} — start {name} by hand")


def main(argv: list[str]) -> int:
    import docker

    if len(argv) not in (3, 4):
        print("usage: python -m deployra_backend.services.self_update <container> <version> [backend|panel|gateway]", file=sys.stderr)
        return 2
    client = docker.from_env(timeout=300)
    data = Path(os.environ.get("DEPLOYRA_DATA", "/var/lib/deployra"))
    component = argv[3] if len(argv) == 4 else "backend"
    return 0 if apply(client.api, data, argv[1], argv[2], component=component) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
