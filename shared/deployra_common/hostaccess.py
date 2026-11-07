"""Dostęp kontenera do hosta: ścieżki systemowe, docker.sock, sieć hosta, tryb uprzywilejowany.

Taki kontener to w praktyce root na węźle (np. `/:/host` albo `docker.sock` pozwalają przejąć serwer), więc panel
pozwala na niego tylko kontom z osobnym, ryzykownym uprawnieniem. Funkcje zwracają listę problemów (pusta = OK).
"""

from __future__ import annotations

import posixpath
from typing import Any

from .i18n import tr

# katalogi systemowe, gniazda silnika kontenerów i dane Deployry na węźle (sekrety, klucze)
CRITICAL = (
    "/etc", "/root", "/proc", "/sys", "/dev", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
    "/var/lib/docker", "/var/lib/containerd", "/var/lib/deployra", "/var/lib/deployra-panel",
    "/var/run/docker.sock", "/run/docker.sock", "/run/containerd", "/var/run/containerd", "/run/podman", "/var/run/podman",
)
ALLOWED_READ_ONLY = {"/etc/localtime", "/etc/timezone"}  # strefa czasowa — częsty i nieszkodliwy wpis tylko do odczytu
HOST_NETWORKS = ("host",)


def _norm(path: str) -> str:
    return posixpath.normpath("/" + path.strip().lstrip("/")) if path.strip() else ""


def host_path_problem(source: str, read_only: bool = False) -> str | None:
    """Ścieżka hosta, która daje dostęp do systemu (sama w sobie albo przez to, co zawiera); None = bezpieczna."""
    if not source or not source.strip().startswith("/"):
        return None  # nazwany wolumen albo ścieżka względna stacka
    path = _norm(source)
    if read_only and path in ALLOWED_READ_ONLY:
        return None
    for critical in CRITICAL:
        if path == "/" or path == critical or path.startswith(critical + "/") or critical.startswith(path + "/"):
            return tr("srv.host.path", path=path)
    return None


def network_problem(network: str | None) -> str | None:
    value = (network or "").strip().lower()
    if value in HOST_NETWORKS:
        return tr("srv.host.network", network=value)
    if value.startswith(("container:", "service:")):
        return tr("srv.host.shared_network", network=value)
    return None


def spec_problems(volumes: list[Any], network: str | None = None) -> list[str]:
    """ContainerSpec / AppSpec: wolumeny (obiekty z `source`, `read_only`) i sieć."""
    out = [p for v in volumes or [] if (p := host_path_problem(getattr(v, "source", ""), bool(getattr(v, "read_only", False))))]
    if problem := network_problem(network):
        out.append(problem)
    return out


def _short_volume(entry: str) -> tuple[str, bool]:
    """`źródło:cel[:tryb]` → (źródło, tylko do odczytu)."""
    parts = entry.split(":")
    if len(parts) < 2:
        return "", False
    return parts[0], len(parts) > 2 and "ro" in parts[2].split(",")


def compose_problems(text: str) -> list[str]:
    """Plik compose: tryb uprzywilejowany, sieć/PID/IPC hosta, dodatkowe uprawnienia jądra, urządzenia, wyłączone
    zabezpieczenia i montowania ścieżek systemowych (krótka i długa składnia oraz wolumeny `driver_opts: device`)."""
    import yaml

    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return []  # składnię sprawdza osobno walidacja compose
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    services = data.get("services") if isinstance(data.get("services"), dict) else {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if svc.get("privileged"):
            out.append(tr("srv.host.privileged", service=name))
        for key in ("network_mode", "pid", "ipc", "userns_mode", "uts", "cgroup"):
            value = str(svc.get(key) or "").strip().lower()
            if value == "host" or (key == "network_mode" and value.startswith(("container:", "service:"))):
                out.append(tr("srv.host.mode", service=name, key=key, value=value))
        if svc.get("cap_add"):
            out.append(tr("srv.host.caps", service=name, caps=", ".join(map(str, svc["cap_add"]))))
        if svc.get("devices"):
            out.append(tr("srv.host.devices", service=name))
        if any("unconfined" in str(opt) or "no-new-privileges:false" in str(opt) for opt in svc.get("security_opt") or []):
            out.append(tr("srv.host.security_opt", service=name))
        for vol in svc.get("volumes") or []:
            if isinstance(vol, str):
                source, ro = _short_volume(vol)
            elif isinstance(vol, dict):
                source, ro = str(vol.get("source") or ""), bool(vol.get("read_only"))
            else:
                continue
            if problem := host_path_problem(source, ro):
                out.append(f"{name}: {problem}")
    volumes = data.get("volumes") if isinstance(data.get("volumes"), dict) else {}
    for name, vol in volumes.items():
        device = str(((vol or {}).get("driver_opts") or {}).get("device") or "") if isinstance(vol, dict) else ""
        if problem := host_path_problem(device):
            out.append(f"{name}: {problem}")
    return out
