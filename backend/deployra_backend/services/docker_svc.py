"""Operacje na Dockerze przez niskopoziomowe API (szybkie listy, bez inspect per kontener)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from deployra_common import container_profiles
from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import ContainerSpec, NetworkSpec, VolumeSpec

SYSTEM_LABEL = "deployra.system"
APP_LABEL = "deployra.app"
PROTECTED_NETWORKS = {"bridge", "host", "none"}
CONTAINER_ACTIONS = {"start", "stop", "restart", "kill", "pause", "unpause"}


def _name(summary: dict) -> str:
    names = summary.get("Names") or []
    return names[0].lstrip("/") if names else summary["Id"][:12]


def _ports(raw: list[dict] | None) -> list[dict]:
    seen, out = set(), []
    for p in raw or []:
        key = (p.get("PublicPort"), p.get("PrivatePort"), p.get("Type"))
        if key in seen:
            continue
        seen.add(key)
        entry = {"container": p.get("PrivatePort"), "proto": p.get("Type", "tcp")}
        if p.get("PublicPort"):
            entry.update(host=p["PublicPort"], host_ip=p.get("IP"))
        out.append(entry)
    return sorted(out, key=lambda e: (e.get("host") or 0, e["container"] or 0))


def list_containers(client: Any) -> list[dict]:
    out = []
    for c in client.api.containers(all=True):
        labels = c.get("Labels") or {}
        out.append(
            {
                "id": c["Id"],
                "short_id": c["Id"][:12],
                "name": _name(c),
                "image": c.get("Image"),
                "image_id": c.get("ImageID"),
                "state": c.get("State"),
                "status": c.get("Status"),
                "created": c.get("Created"),
                "ports": _ports(c.get("Ports")),
                "app": labels.get(APP_LABEL),
                "stack": labels.get("com.docker.compose.project"),
                "service": labels.get("com.docker.compose.service"),
                "system": labels.get(SYSTEM_LABEL),
                "mounts": [
                    {"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"), "destination": m.get("Destination")}
                    for m in c.get("Mounts") or []
                ],
                "networks": sorted(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
            }
        )
    return sorted(out, key=lambda c: c["name"])


def inspect_container(client: Any, cid: str) -> dict:
    return client.api.inspect_container(cid)


def container_action(client: Any, cid: str, action: str) -> None:
    if action not in CONTAINER_ACTIONS:
        raise ValueError(tr("node.unknown_action", action=action))
    getattr(client.api, action)(cid)


def remove_container(client: Any, cid: str, *, force: bool, volumes: bool) -> None:
    info = client.api.inspect_container(cid)
    if (info.get("Config", {}).get("Labels") or {}).get(SYSTEM_LABEL):
        raise PermissionError(tr("node.system_container"))
    client.api.remove_container(cid, force=force, v=volumes)


def container_top(client: Any, cid: str) -> dict:
    return client.api.top(cid)


# --- statystyki ---------------------------------------------------------------


def compute_stats(cur: dict, prev: dict | None) -> dict:
    cpu = 0.0
    if prev:
        cur_cpu, prev_cpu = cur.get("cpu_stats") or {}, prev.get("cpu_stats") or {}
        cpu_delta = (cur_cpu.get("cpu_usage") or {}).get("total_usage", 0) - (prev_cpu.get("cpu_usage") or {}).get(
            "total_usage", 0
        )
        sys_delta = cur_cpu.get("system_cpu_usage", 0) - prev_cpu.get("system_cpu_usage", 0)
        online = cur_cpu.get("online_cpus") or len((cur_cpu.get("cpu_usage") or {}).get("percpu_usage") or [1])
        if sys_delta > 0 and cpu_delta >= 0:
            cpu = cpu_delta / sys_delta * online * 100
    mem = cur.get("memory_stats") or {}
    details = mem.get("stats") or {}
    usage = max(mem.get("usage", 0) - details.get("inactive_file", details.get("cache", 0)), 0)
    limit = mem.get("limit") or 0
    rx = tx = 0
    for net in (cur.get("networks") or {}).values():
        rx += net.get("rx_bytes", 0)
        tx += net.get("tx_bytes", 0)
    read = write = 0
    for entry in (cur.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
        if entry.get("op", "").lower() == "read":
            read += entry.get("value", 0)
        elif entry.get("op", "").lower() == "write":
            write += entry.get("value", 0)
    return {
        "cpu": round(cpu, 2),
        "mem_usage": usage,
        "mem_limit": limit,
        "mem": round(usage / limit * 100, 2) if limit else 0.0,
        "net_rx": rx,
        "net_tx": tx,
        "blk_read": read,
        "blk_write": write,
        "pids": (cur.get("pids_stats") or {}).get("current"),
    }


def stats_once(client: Any, cid: str) -> dict:
    """Jedna próbka z prawdziwym CPU% (daemon zbiera dwie próbki, ~1-2 s)."""
    raw = client.api.stats(cid, stream=False)
    return compute_stats(raw, raw.get("precpu_stats") and {"cpu_stats": raw["precpu_stats"]})


async def stats_stream(client: Any, cid: str, interval: float = 2.0) -> AsyncIterator[dict]:
    prev = None
    while True:
        raw = await asyncio.to_thread(client.api.stats, cid, stream=False, one_shot=True)
        yield compute_stats(raw, prev)
        prev = raw
        await asyncio.sleep(interval)


# --- logi ---------------------------------------------------------------------


async def follow_logs(client: Any, cid: str, *, tail: int = 200, follow: bool = True, timestamps: bool = False) -> AsyncIterator[str]:
    """Linie logów; przy rozłączeniu klienta zamyka strumień Dockera (bez wiszących wątków)."""
    try:
        stream = await asyncio.to_thread(
            client.api.logs, cid, stream=True, follow=follow, tail=tail, timestamps=timestamps
        )
    except Exception as exc:
        yield f"⚠️ Błąd odczytu logów Dockera: {exc}"
        return
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1000)

    def pump() -> None:
        try:
            for chunk in stream:
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
        except Exception:  # noqa: BLE001 — zamknięty strumień kończy pompę
            pass
        finally:
            try:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=pump, name=f"logs-{cid[:12]}", daemon=True).start()
    buffer = b""
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                yield line.rstrip(b"\r").decode(errors="replace")
        if buffer:
            yield buffer.decode(errors="replace")
    finally:
        close = getattr(stream, "close", None)
        if close:
            await asyncio.to_thread(close)


# --- obrazy -------------------------------------------------------------------


def list_images(client: Any) -> list[dict]:
    used: dict[str, list[str]] = {}
    for c in client.api.containers(all=True):
        used.setdefault(c.get("ImageID", ""), []).append(_name(c))
    out = []
    for img in client.api.images():
        tags = [t for t in img.get("RepoTags") or [] if t != "<none>:<none>"]
        out.append(
            {
                "id": img["Id"],
                "short_id": img["Id"].split(":")[-1][:12],
                "tags": tags,
                "size": img.get("Size", 0),
                "created": img.get("Created"),
                "used_by": used.get(img["Id"], []),
                "dangling": not tags,
            }
        )
    return sorted(out, key=lambda i: (i["tags"][0] if i["tags"] else "~", i["id"]))


def pull_image(client: Any, job: Job, image: str, auth_config: dict | None = None) -> str:
    """Pobiera obraz, logując zmiany statusu warstw (bez zalewania logu paskiem postępu)."""
    from docker.utils import parse_repository_tag

    repo, tag = parse_repository_tag(image)
    tag = tag or "latest"
    job.log(tr("node.pulling", image=f"{repo}:{tag}"))
    last: dict[str, str] = {}
    for event in client.api.pull(repo, tag=tag, stream=True, decode=True, auth_config=auth_config):
        if "error" in event:
            raise JobFailed(event.get("error") or tr("node.pull_error"))
        status, layer = event.get("status", ""), event.get("id", "")
        if not layer:
            job.log(status)
        elif last.get(layer) != status and status not in ("Downloading", "Extracting", "Waiting"):
            job.log(f"{layer}: {status}")
        last[layer] = status
    return f"{repo}:{tag}"


def remove_image(client: Any, image_id: str, force: bool = False) -> None:
    client.api.remove_image(image_id, force=force)


def prune_images(client: Any, all_unused: bool = False) -> dict:
    filters = {"dangling": False} if all_unused else {"dangling": True}
    return client.api.prune_images(filters=filters)


# --- wolumeny i sieci -----------------------------------------------------------


def list_volumes(client: Any) -> list[dict]:
    used: dict[str, list[str]] = {}
    for c in client.api.containers(all=True):
        for m in c.get("Mounts") or []:
            if m.get("Type") == "volume" and m.get("Name"):
                used.setdefault(m["Name"], []).append(_name(c))
    volumes = (client.api.volumes() or {}).get("Volumes") or []
    out = [
        {
            "name": v["Name"],
            "driver": v.get("Driver"),
            "mountpoint": v.get("Mountpoint"),
            "created": v.get("CreatedAt"),
            "labels": v.get("Labels") or {},
            "used_by": used.get(v["Name"], []),
        }
        for v in volumes
    ]
    return sorted(out, key=lambda v: v["name"])


def create_volume(client: Any, spec: VolumeSpec) -> dict:
    return client.api.create_volume(spec.name, driver=spec.driver, driver_opts=spec.driver_opts or None, labels=spec.labels or None)


def list_networks(client: Any) -> list[dict]:
    members: dict[str, list[str]] = {}
    for c in client.api.containers(all=True):
        for net in ((c.get("NetworkSettings") or {}).get("Networks") or {}).values():
            if net.get("NetworkID"):
                members.setdefault(net["NetworkID"], []).append(_name(c))
    out = []
    for n in client.api.networks():
        ipam = [cfg.get("Subnet") for cfg in ((n.get("IPAM") or {}).get("Config") or []) if cfg.get("Subnet")]
        out.append(
            {
                "id": n["Id"],
                "short_id": n["Id"][:12],
                "name": n["Name"],
                "driver": n.get("Driver"),
                "scope": n.get("Scope"),
                "internal": n.get("Internal", False),
                "attachable": n.get("Attachable", False),
                "subnets": ipam,
                "containers": members.get(n["Id"], []),
                "protected": n["Name"] in PROTECTED_NETWORKS,
            }
        )
    return sorted(out, key=lambda n: n["name"])


def create_network(client: Any, spec: NetworkSpec) -> dict:
    from docker.types import IPAMConfig, IPAMPool

    ipam = None
    if spec.subnet:
        ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=spec.subnet, gateway=spec.gateway)])
    return client.api.create_network(
        spec.name,
        driver=spec.driver,
        internal=spec.internal,
        attachable=spec.attachable,
        enable_ipv6=spec.enable_ipv6,
        labels=spec.labels or None,
        options=spec.options or None,
        ipam=ipam,
    )


def remove_network(client: Any, network_id: str) -> None:
    info = client.api.inspect_network(network_id)
    if info.get("Name") in PROTECTED_NETWORKS:
        raise PermissionError(tr("node.network_protected"))
    client.api.remove_network(network_id)


def ensure_network(client: Any, name: str) -> None:
    if not client.api.networks(names=[name]):
        client.api.create_network(name, driver="bridge", attachable=True, labels={SYSTEM_LABEL: "network"})


# --- uruchamianie kontenerów -------------------------------------------------


def run_container(client: Any, spec: ContainerSpec) -> str:
    """Tworzy i uruchamia kontener z bezpiecznej specyfikacji. Zwraca id."""
    if spec.network:
        ensure_network(client, spec.network)
    port_bindings: dict[str, Any] = {}
    exposed = []
    for p in spec.ports:
        key = f"{p.container}/{p.protocol}"
        exposed.append((p.container, p.protocol))
        if p.host:
            port_bindings[key] = (p.host_ip, p.host) if p.host_ip else p.host
    binds = {v.source: {"bind": v.target, "mode": "ro" if v.read_only else "rw"} for v in spec.volumes}
    restart = None if spec.restart == "no" else {"Name": spec.restart, **({"MaximumRetryCount": 5} if spec.restart == "on-failure" else {})}
    host_config = client.api.create_host_config(
        port_bindings=port_bindings or None,
        binds=binds or None,
        restart_policy=restart,
        mem_limit=f"{spec.memory_mb}m" if spec.memory_mb else None,
        nano_cpus=int(spec.cpus * 1e9) if spec.cpus else None,
        network_mode=spec.network or None,
        log_config={"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
    )
    container = client.api.create_container(
        spec.image,
        command=spec.command,
        working_dir=spec.working_dir,
        name=spec.name,
        environment=spec.env or None,
        labels=spec.labels or None,
        ports=[p if proto == "tcp" else (p, proto) for p, proto in exposed] or None,
        host_config=host_config,
    )
    client.api.start(container["Id"])
    return container["Id"]


def find_container(client: Any, name: str) -> dict | None:
    for c in client.api.containers(all=True, filters={"name": f"^/{name}$"}):
        return c
    return None


def get_container_profile(client: Any, cid: str) -> dict:
    """Zwraca profil kontenera i listę dostępnych szybkich akcji."""
    info = client.api.inspect_container(cid)
    cfg = info.get("Config") or {}
    img = cfg.get("Image") or ""
    env = cfg.get("Env") or []
    ports = []
    for p in (cfg.get("ExposedPorts") or {}).keys():
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    for p in ((info.get("NetworkSettings") or {}).get("Ports") or {}).keys():
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    p = container_profiles.detect_profile(img, env, ports)
    return {
        "kind": p.kind,
        "title": p.title,
        "category": p.category,
        "icon": p.icon,
        "color": p.color,
        "can_dump": p.can_dump,
        "actions": [
            {
                "id": a.id,
                "label": a.label,
                "icon": a.icon,
                "style": a.style,
                "confirm": a.confirm,
                "is_dump": a.is_dump,
                "description": a.description,
            }
            for a in p.actions
        ],
    }


def run_quick_action(client: Any, cid: str, action_id: str) -> tuple[int, str | bytes, bool, str]:
    """Wykonywanie szybkiej akcji w kontenerze. Zwraca (kod_wyjścia, wynik, czy_plik, nazwa_pliku)."""
    import gzip

    info = client.api.inspect_container(cid)
    cfg = info.get("Config") or {}
    img = cfg.get("Image") or ""
    env = cfg.get("Env") or []
    ports = []
    for p in (cfg.get("ExposedPorts") or {}).keys():
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    for p in ((info.get("NetworkSettings") or {}).get("Ports") or {}).keys():
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    profile = container_profiles.detect_profile(img, env, ports)
    cmd, is_dump, filename = container_profiles.build_action_command(profile, action_id, env)

    exec_res = client.api.exec_create(cid, cmd)
    exec_id = exec_res["Id"]
    output = client.api.exec_start(exec_id)
    exit_code = (client.api.exec_inspect(exec_id) or {}).get("ExitCode", 0)

    if is_dump:
        raw_bytes = output if isinstance(output, bytes) else str(output).encode()
        compressed = gzip.compress(raw_bytes)
        return exit_code, compressed, True, filename

    out_str = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
    return exit_code, out_str, False, ""


# --- Zadania cykliczne: jednorazowe polecenie w działającym kontenerze -------------------------

EXEC_OUTPUT_LIMIT = 64 * 1024
# `timeout` (coreutils/busybox) ubija polecenie po limicie; bez niego polecenie działa, a panel przestaje czekać
EXEC_WITH_TIMEOUT = 'T="$1"; shift; if command -v timeout >/dev/null 2>&1; then exec timeout -s KILL "$T" "$@"; fi; exec "$@"'
TIMEOUT_CODES = (124, 137, 143)  # kody `timeout` i SIGKILL/SIGTERM po przekroczeniu limitu


def exec_command(client: Any, cid: str, cmd: list[str], timeout: int) -> dict:
    """Polecenie bez TTY: kod wyjścia, ogon wyniku (do 64 KB) i czas trwania; `timed_out` gdy minął limit."""
    import time

    started = time.monotonic()
    code, output = _exec(client, cid, ["sh", "-c", EXEC_WITH_TIMEOUT, "--", str(timeout), *cmd])
    elapsed = time.monotonic() - started
    return {
        "exit_code": code,
        "output": output[-EXEC_OUTPUT_LIMIT:],
        "duration": round(elapsed, 2),
        "timed_out": code in TIMEOUT_CODES and elapsed >= timeout - 1,
    }


def restore_dump(client: Any, cid: str, payload: bytes) -> dict:
    """Przywraca zrzut bazy (gzip, tak jak zwraca `dump_sql` / `dump_archive`) do działającego kontenera.

    Zrzut trafia do /tmp w kontenerze przez put_archive; polecenie z profilu czyta go i usuwa plik.
    """
    import gzip
    import uuid

    info = client.api.inspect_container(cid)
    if not (info.get("State") or {}).get("Running"):
        raise ValueError("Przywracanie wymaga działającego kontenera bazy danych")
    cfg = info.get("Config") or {}
    env = cfg.get("Env") or []
    ports = []
    for p in list((cfg.get("ExposedPorts") or {}).keys()) + list(((info.get("NetworkSettings") or {}).get("Ports") or {}).keys()):
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    profile = container_profiles.detect_profile(cfg.get("Image") or "", env, ports)
    try:
        raw = gzip.decompress(payload)
    except OSError as exc:
        raise ValueError(f"Plik kopii nie jest poprawnym archiwum gzip: {exc}") from exc

    remote = f"/tmp/deployra-restore-{uuid.uuid4().hex}"
    write_container_file(client, cid, remote, raw)
    try:
        command = container_profiles.build_restore_command(profile, env, remote)
    except ValueError:
        _exec(client, cid, ["rm", "-f", remote])
        raise
    code, output = _exec(client, cid, command)
    return {"exit_code": code, "output": output[-2000:]}


# --- Menedżer plików kontenera ------------------------------------------------
# Działający kontener: system plików kontenera przez `exec` (listowanie, usuwanie, mkdir) i archiwa Dockera
# (odczyt/zapis). Zatrzymany albo bez powłoki: tylko podpięte foldery (bind/wolumen) — bezpośrednio, gdy leżą
# w /srv/deployra (ta sama ścieżka w backendzie), w pozostałych przypadkach jednorazowym kontenerem
# `alpine` z --volumes-from (backend nie widzi reszty dysku hosta).

FILES_HELPER = "alpine:3.20"
PROTECTED = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sys", "/usr", "/var")
LIST_SCRIPT = (
    'TARGET="$1"; cd "$TARGET" 2>/dev/null || exit 2; '
    "for f in .* *; do "
    '[ "$f" = "." ] || [ "$f" = ".." ] && continue; '
    '[ ! -e "$f" ] && [ ! -L "$f" ] && continue; '
    'if [ -d "$f" ]; then T="dir"; S=0; '
    'elif [ -L "$f" ]; then T="link"; S=0; '
    'else T="file"; S=$(wc -c < "$f" 2>/dev/null || echo 0); fi; '
    'printf "%s\\t%s\\t%s\\n" "$T" "$S" "$f"; '
    "done"
)
NO_SHELL = (126, 127)  # exec: brak `sh` w obrazie (distroless, scratch)


class StoppedContainer(ValueError):
    """Ścieżka poza podpiętymi folderami, a kontener nie działa."""


def _clean(path: str) -> str:
    import posixpath

    norm = posixpath.normpath("/" + (path or "/").strip("/"))
    return "/" if norm in ("", ".", "//") else norm


def container_mounts(info: dict) -> list[dict]:
    return [
        {"type": m.get("Type") or "volume", "name": m.get("Name") or "", "source": m.get("Source") or "",
         "destination": m.get("Destination") or "", "rw": bool(m.get("RW", True))}
        for m in info.get("Mounts") or [] if m.get("Destination")
    ]  # fmt: skip


def mount_for(mounts: list[dict], path: str) -> dict | None:
    """Najgłębszy mount, pod którym leży ścieżka."""
    found = [m for m in mounts if path == m["destination"] or path.startswith(m["destination"].rstrip("/") + "/")]
    return max(found, key=lambda m: len(m["destination"]), default=None)


def _local_dir(mount: dict, path: str, srv_dir: Any) -> Any:
    """Ścieżka hosta dla bindu z /srv/deployra (backend ma ją pod tą samą ścieżką) albo None."""
    from pathlib import Path

    if not srv_dir or mount["type"] != "bind" or not mount["source"]:
        return None
    root = Path(srv_dir).resolve()
    source = Path(mount["source"])
    try:
        source.resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    rest = path[len(mount["destination"].rstrip("/")):].lstrip("/")
    target = (source / rest).resolve() if rest else source.resolve()
    try:
        target.relative_to(source.resolve())  # symlink nie wyprowadzi poza folder
    except ValueError:
        return None
    return target


def _exec(client: Any, cid: str, cmd: list[str]) -> tuple[int, str]:
    exec_id = client.api.exec_create(cid, cmd)["Id"]
    output = client.api.exec_start(exec_id)
    code = (client.api.exec_inspect(exec_id) or {}).get("ExitCode", 0) or 0
    return int(code), output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)


def _helper(client: Any, cid: str, cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """Jednorazowy alpine z wolumenami i bindami kontenera (działa także dla zatrzymanego)."""
    try:
        client.api.inspect_image(FILES_HELPER)
    except Exception:  # noqa: BLE001 — pierwszy raz na węźle
        client.api.pull(FILES_HELPER)
    host_config = client.api.create_host_config(volumes_from=[cid], network_mode="none")
    hid = client.api.create_container(FILES_HELPER, command=cmd, labels={SYSTEM_LABEL: "files"}, host_config=host_config)["Id"]
    try:
        client.api.start(hid)
        code = (client.api.wait(hid, timeout=timeout) or {}).get("StatusCode", 1)
        out = client.api.logs(hid, stdout=True, stderr=True)
    finally:
        try:
            client.api.remove_container(hid, force=True)
        except Exception:  # noqa: BLE001 — AutoRemove albo już go nie ma
            pass
    return int(code), out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)


def _parse_listing(text: str, directory: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        parts = line.rstrip("\r").split("\t", 2)
        if len(parts) != 3 or parts[0] not in ("dir", "file", "link"):
            continue
        kind, size, name = parts
        items.append({"name": name, "path": f"{directory.rstrip('/')}/{name}", "type": kind, "is_dir": kind == "dir",
                      "size": int(size) if size.strip().isdigit() else 0})  # fmt: skip
    return items


def _scan(directory: Any, shown: str) -> list[dict]:
    items = []
    for entry in directory.iterdir():
        kind = "dir" if entry.is_dir() else "link" if entry.is_symlink() else "file"
        size = entry.lstat().st_size if kind == "file" else 0
        items.append({"name": entry.name, "path": f"{shown.rstrip('/')}/{entry.name}", "type": kind, "is_dir": kind == "dir", "size": size})
    return items


def list_container_files(client: Any, cid: str, path: str = "/", srv_dir: Any = None) -> dict:
    """Zawartość katalogu: system plików kontenera albo (failover) podpięte foldery."""
    target = _clean(path)
    info = client.api.inspect_container(cid)
    mounts = container_mounts(info)
    running = bool((info.get("State") or {}).get("Running"))
    result = {"path": target, "mounts": mounts, "running": running, "mode": "container"}

    if running:
        try:
            code, out = _exec(client, cid, ["sh", "-c", LIST_SCRIPT, "--", target])
        except Exception as exc:  # noqa: BLE001 — np. kontener zatrzymał się w międzyczasie
            code, out = NO_SHELL[0], str(exc)
        if code == 2:
            raise FileNotFoundError(tr("node.files.no_dir", path=target))
        if code == 0:
            items = _parse_listing(out, target)
            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return {**result, "items": items}
        if code not in NO_SHELL:
            raise RuntimeError(tr("node.files.exec_failed", error=out.strip()[:300]))

    # failover: tylko podpięte foldery
    result["mode"] = "mounts"
    mount = mount_for(mounts, target)
    if not mount:
        items = [{"name": m["destination"], "path": m["destination"], "type": "mount", "is_dir": True, "size": 0, "mount": m}
                 for m in sorted(mounts, key=lambda m: m["destination"])]  # fmt: skip
        return {**result, "path": "/", "items": items}
    local = _local_dir(mount, target, srv_dir)
    if local is not None:
        if not local.is_dir():
            raise FileNotFoundError(tr("node.files.no_dir", path=target))
        items = _scan(local, target)
    else:
        code, out = _helper(client, cid, ["sh", "-c", LIST_SCRIPT, "--", target])
        if code == 2:
            raise FileNotFoundError(tr("node.files.no_dir", path=target))
        if code != 0:
            raise RuntimeError(tr("node.files.exec_failed", error=out.strip()[:300]))
        items = _parse_listing(out, target)
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {**result, "items": items}


def _archive(client: Any, cid: str, path: str) -> tuple[Any, dict]:
    try:
        return client.api.get_archive(cid, path)
    except Exception as exc:  # noqa: BLE001 — brak pliku albo uprawnień
        raise FileNotFoundError(tr("node.files.cant_read", path=path, error=exc)) from exc


def _first_member(strm: Any) -> tuple[Any, bytes | None]:
    """Pierwszy plik z archiwum Dockera (strumień kawałków)."""
    import io
    import tarfile

    buffer = io.BytesIO()
    for chunk in strm:
        buffer.write(chunk)
    buffer.seek(0)
    tar = tarfile.open(fileobj=buffer, mode="r")
    member = tar.next()
    if member is None:
        raise FileNotFoundError(tr("node.files.empty_archive"))
    data = tar.extractfile(member).read() if member.isfile() else None
    return member, data


def read_container_file(client: Any, cid: str, path: str, max_bytes: int = 2 * 1024 * 1024) -> dict:
    """Odczyt pliku przez archiwum Dockera — działa także na zatrzymanym kontenerze."""
    import posixpath

    clean_path = _clean(path)
    strm, stat = _archive(client, cid, clean_path)
    if stat.get("size", 0) > 64 * 1024 * 1024:
        raise ValueError(tr("node.files.too_big"))
    member, raw = _first_member(strm)
    if member.isdir():
        raise IsADirectoryError(tr("node.files.is_dir", path=clean_path))
    raw = raw or b""
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    is_binary = b"\x00" in raw[:4096]
    return {
        "path": clean_path, "name": stat.get("name") or posixpath.basename(clean_path), "size": stat.get("size", len(raw)),
        "is_binary": is_binary, "truncated": truncated, "content": "" if is_binary else raw.decode("utf-8", errors="replace"),
    }  # fmt: skip


def get_container_file_bytes(client: Any, cid: str, path: str) -> tuple[str, bytes]:
    """Surowe bajty pliku do pobrania; folder → archiwum ZIP z całą zawartością."""
    import io
    import posixpath
    import tarfile
    import zipfile

    clean_path = _clean(path)
    strm, stat = _archive(client, cid, clean_path)
    name = stat.get("name") or posixpath.basename(clean_path) or "root"
    buffer = io.BytesIO()
    for chunk in strm:
        buffer.write(chunk)
    buffer.seek(0)
    tar = tarfile.open(fileobj=buffer, mode="r")
    first = tar.next()
    if first is None:
        raise FileNotFoundError(tr("node.files.empty_archive"))
    if not first.isdir():
        data = tar.extractfile(first).read() if first.isfile() else None
        if data is None:
            raise FileNotFoundError(tr("node.files.not_file", path=clean_path))
        return name, data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in [first, *tar]:
            if member.isdir():
                zf.writestr(member.name.rstrip("/") + "/", b"")
            elif member.isfile():
                zf.writestr(member.name, tar.extractfile(member).read())  # dowiązania i urządzenia pomijamy
    return f"{name}.zip", out.getvalue()


def _rel_parts(rel: str) -> list[str]:
    """Ścieżka względna z przeglądarki (wgrywanie folderu) → bezpieczne segmenty, bez `..`."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(tr("node.files.bad_path"))
    return parts


def upload_container_tree(client: Any, cid: str, directory: str, files: list[tuple[str, bytes]]) -> list[dict]:
    """Wiele plików (także z podfolderami) jednym archiwum — put_archive sam tworzy brakujące foldery."""
    import io
    import tarfile
    import time

    target = _clean(directory)
    uid, gid = _default_owner(client.api.inspect_container(cid))
    now = int(time.time())
    tar_stream, dirs, done = io.BytesIO(), set(), []
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        for rel, data in files:
            parts = _rel_parts(rel)
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder not in dirs:
                    dirs.add(folder)
                    info = tarfile.TarInfo(name=folder)
                    info.type, info.mode, info.mtime, info.uid, info.gid = tarfile.DIRTYPE, 0o755, now, uid, gid
                    tar.addfile(info)
            info = tarfile.TarInfo(name="/".join(parts))
            info.size, info.mtime, info.mode, info.uid, info.gid = len(data), now, 0o644, uid, gid
            tar.addfile(info, io.BytesIO(data))
            done.append({"name": "/".join(parts), "size": len(data)})
    client.api.put_archive(cid, target, tar_stream.getvalue())
    return done


def _owner(client: Any, cid: str, path: str) -> tuple[int, int, int] | None:
    """(uid, gid, tryb) istniejącego pliku — zapis nie zmienia właściciela ani uprawnień."""
    try:
        strm, stat = client.api.get_archive(cid, path)
    except Exception:  # noqa: BLE001 — nowy plik
        return None
    if stat.get("size", 0) > 64 * 1024 * 1024:
        return None
    try:
        member, _ = _first_member(strm)
    except Exception:  # noqa: BLE001
        return None
    return (member.uid, member.gid, member.mode) if member.isfile() else None


def _default_owner(info: dict) -> tuple[int, int]:
    """Nowe pliki dostają użytkownika kontenera (Config.User „1000:1000”), jeśli jest liczbowy."""
    user = ((info.get("Config") or {}).get("User") or "").split(":")
    uid = int(user[0]) if user[0].isdigit() else 0
    gid = int(user[1]) if len(user) > 1 and user[1].isdigit() else uid
    return uid, gid


def write_container_file(client: Any, cid: str, path: str, content: str | bytes) -> dict:
    """Zapis pliku przez put_archive (także w zatrzymanym kontenerze); zachowuje właściciela i tryb."""
    import io
    import posixpath
    import tarfile
    import time

    clean_path = _clean(path)
    if clean_path == "/":
        raise ValueError(tr("node.files.bad_path"))
    data = content.encode("utf-8") if isinstance(content, str) else content
    directory, filename = posixpath.split(clean_path)
    existing = _owner(client, cid, clean_path)
    if existing:
        uid, gid, mode = existing
    else:
        uid, gid = _default_owner(client.api.inspect_container(cid))
        mode = 0o644
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size, info.mtime, info.mode, info.uid, info.gid = len(data), int(time.time()), mode, uid, gid
        tar.addfile(info, io.BytesIO(data))
    client.api.put_archive(cid, directory or "/", tar_stream.getvalue())
    return {"ok": True, "path": clean_path, "size": len(data)}


def _run_anywhere(client: Any, cid: str, cmd: list[str], path: str) -> None:
    """Polecenie na ścieżce: w działającym kontenerze `exec`, w zatrzymanym — pomocnik na podpiętym folderze."""
    info = client.api.inspect_container(cid)
    running = bool((info.get("State") or {}).get("Running"))
    if running:
        code, out = _exec(client, cid, cmd)
        if code not in NO_SHELL:
            if code != 0:
                raise RuntimeError(tr("node.files.exec_failed", error=out.strip()[:300]))
            return
    mount = mount_for(container_mounts(info), path)
    if not mount or path == mount["destination"]:
        raise StoppedContainer(tr("node.files.stopped_only_mounts"))
    if not mount["rw"]:
        raise PermissionError(tr("node.files.read_only", path=mount["destination"]))
    code, out = _helper(client, cid, cmd)
    if code != 0:
        raise RuntimeError(tr("node.files.exec_failed", error=out.strip()[:300]))


def delete_container_file(client: Any, cid: str, path: str) -> dict:
    clean_path = _clean(path)
    if clean_path in PROTECTED:
        raise ValueError(tr("node.files.protected", path=clean_path))
    _run_anywhere(client, cid, ["rm", "-rf", "--", clean_path], clean_path)
    return {"ok": True, "path": clean_path}


def mkdir_container(client: Any, cid: str, path: str) -> dict:
    clean_path = _clean(path)
    _run_anywhere(client, cid, ["mkdir", "-p", "--", clean_path], clean_path)
    return {"ok": True, "path": clean_path}
