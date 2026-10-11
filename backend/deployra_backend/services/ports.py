"""Pula portów hosta dla aplikacji (bramka łączy się z węzłem na te porty przez tunel)."""

from __future__ import annotations

import socket
from typing import Any

from deployra_common.i18n import tr

from ..state import State


def _port_free(port: int) -> bool:
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((addr, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (97, 10047):  # brak IPv6
                continue
            return False
    return True


def docker_ports(client: Any) -> set[int]:
    used = set()
    for c in client.api.containers(all=True):
        for p in c.get("Ports") or []:
            if p.get("PublicPort"):
                used.add(int(p["PublicPort"]))
    return used


def allocate(
    state: State, client: Any, key: str, pool: tuple[int, int], preferred: int | None = None, check_host: bool = True
) -> int:
    """Zwraca port przypisany do `key` (aplikacji); przydziela nowy z puli, jeśli trzeba.

    `check_host=False` dla CT na Proxmoxie — porty CT nie zajmują portów hosta.
    """
    current = state.get("ports", {})
    if key in current and (preferred is None or current[key] == preferred):
        return int(current[key])
    taken = {int(p) for k, p in current.items() if k != key} | docker_ports(client)
    candidates = [preferred] if preferred else range(pool[0], pool[1] + 1)
    for port in candidates:
        if port not in taken and (not check_host or _port_free(port)):
            state.mutate("ports", lambda ports: {**ports, key: port})
            return port
    raise RuntimeError(tr("node.no_free_port") if not preferred else tr("node.port_taken", port=preferred))


def release(state: State, key: str) -> None:
    state.mutate("ports", lambda ports: {k: v for k, v in ports.items() if k != key})
