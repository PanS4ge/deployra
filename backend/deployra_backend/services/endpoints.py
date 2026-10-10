"""Endpointy Dockera węzła: host oraz (na Proxmoxie) każdy kontener LXC z własnym Dockerem.

Na hoście Proxmoxa nie trzeba instalować agenta w każdym CT: backend znajduje
działające kontenery LXC po cgroupach (`/proc/*/cgroup` zawiera `/lxc/<vmid>/`),
a z Dockerem w środku rozmawia przez gniazdo widoczne z hosta jako
`/proc/<pid>/root/run/docker.sock`. Nazwy CT bierze z `/etc/pve/lxc/<vmid>.conf`,
adres IP z tablicy routingu namespace'u CT (`/proc/<pid>/net/fib_trie`).
"""

from __future__ import annotations

import ipaddress
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOCAL = "local"
_EP = re.compile(r"^(local|ct-\d{1,9})$")
_LXC_CGROUP = re.compile(r"/lxc(?:\.payload)?[./](\d+)(?:/|$)")
SOCKETS = ("run/docker.sock", "var/run/docker.sock")


@dataclass
class Endpoint:
    id: str
    name: str
    kind: str  # host | lxc
    socket: str | None  # None = domyślny klient z otoczenia
    vmid: int | None = None
    pid: int | None = None
    ip: str | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind, "vmid": self.vmid, "ip": self.ip}


def check_id(value: str | None) -> str:
    value = (value or LOCAL).strip()
    if not _EP.match(value):
        raise ValueError("nieprawidłowy endpoint (local albo ct-<vmid>)")
    return value


def _ct_name(pve_dir: Path, vmid: int) -> str:
    conf = pve_dir / f"{vmid}.conf"
    try:
        for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("["):
                break  # dalej są snapshoty
            if line.startswith("hostname:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return f"CT {vmid}"


def _ct_ip(proc: Path, pid: int) -> str | None:
    """Pierwszy niepętlowy adres IPv4 z /proc/<pid>/net/fib_trie (LOCAL)."""
    try:
        lines = (proc / str(pid) / "net" / "fib_trie").read_text().splitlines()
    except OSError:
        return None
    last = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("|--", "+--")):
            candidate = stripped[3:].strip().split()[0]
            try:
                last = ipaddress.ip_address(candidate)
            except ValueError:
                last = None
        elif "/32 host LOCAL" in stripped and last is not None:
            if not last.is_loopback and not last.is_link_local:
                return str(last)
    return None


def discover_lxc(
    proc: Path = Path("/proc"), pve_dir: Path = Path("/etc/pve/lxc"), probe: Any = Path.is_socket
) -> list[Endpoint]:
    """Działające CT z dostępnym Dockerem (najniższy PID z cgroupy CT = jego init)."""
    first_pid: dict[int, int] = {}
    try:
        entries = os.listdir(proc)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            cgroup = (proc / entry / "cgroup").read_text()
        except OSError:
            continue
        match = _LXC_CGROUP.search(cgroup)
        if match:
            vmid, pid = int(match.group(1)), int(entry)
            if vmid not in first_pid or pid < first_pid[vmid]:
                first_pid[vmid] = pid
    found = []
    for vmid, pid in sorted(first_pid.items()):
        for rel in SOCKETS:
            sock = proc / str(pid) / "root" / rel
            try:
                if probe(sock):
                    found.append(Endpoint(f"ct-{vmid}", _ct_name(pve_dir, vmid), "lxc", str(sock), vmid, pid, _ct_ip(proc, pid)))
                    break
            except OSError:
                continue
    return found


class EndpointRegistry:
    def __init__(self, proxmox: bool, client_factory: Any, ttl: float = 20.0, proc: Path = Path("/proc"),
                 pve_dir: Path = Path("/etc/pve/lxc")) -> None:  # fmt: skip
        self.proxmox = proxmox
        self.client_factory = client_factory  # (socket | None) -> DockerClient
        self.ttl = ttl
        self.proc, self.pve_dir = proc, pve_dir
        self._lock = threading.Lock()
        self._cache: tuple[float, list[Endpoint]] = (0.0, [])
        self._clients: dict[str, tuple[str | None, Any]] = {}

    def list(self, refresh: bool = False) -> list[Endpoint]:
        host = Endpoint(LOCAL, "Host", "host", None)
        if not self.proxmox:
            return [host]
        # Na Proxmoxie Docker hosta jest opcjonalny (wiele osób go tam nie instaluje)
        host_docker = bool(os.environ.get("DOCKER_HOST")) or Path("/var/run/docker.sock").exists()
        with self._lock:
            stamp, cached = self._cache
            if refresh or time.time() - stamp > self.ttl:
                cached = discover_lxc(self.proc, self.pve_dir)
                self._cache = (time.time(), cached)
        return [host, *cached] if host_docker else cached

    def get(self, endpoint_id: str | None) -> Endpoint:
        endpoint_id = check_id(endpoint_id)
        for ep in self.list():
            if ep.id == endpoint_id:
                return ep
        for ep in self.list(refresh=True):
            if ep.id == endpoint_id:
                return ep
        raise LookupError(f"endpoint {endpoint_id} nie istnieje albo CT nie działa")

    def client(self, endpoint_id: str | None) -> Any:
        ep = self.get(endpoint_id)
        with self._lock:
            cached = self._clients.get(ep.id)
            if cached and cached[0] == ep.socket:
                return cached[1]
            client = self.client_factory(ep.socket)
            self._clients[ep.id] = (ep.socket, client)
            return client

    def docker_host_env(self, endpoint_id: str | None) -> dict[str, str]:
        """Zmienna DOCKER_HOST dla poleceń CLI (compose/build) na danym endpoincie."""
        ep = self.get(endpoint_id)
        return {"DOCKER_HOST": f"unix://{ep.socket}"} if ep.socket else {}
