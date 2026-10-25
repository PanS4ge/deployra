"""Pula portów hosta dla aplikacji (bramka łączy się z węzłem na te porty przez tunel)."""

from __future__ import annotations

import socket
from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import JobFailed

from ..state import State


class PortTaken(JobFailed, RuntimeError):
    """Port hosta zajęty — w zadaniu czytelny komunikat zamiast śladu stosu, w API 409."""


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


def _owned(container: dict, key: str | None) -> bool:
    """Kontener tej aplikacji (zwykły, `-next` z wdrożenia bez przestojów, stack compose) — jego port i tak zastąpimy."""
    if not key:
        return False
    labels = container.get("Labels") or {}
    names = {n.lstrip("/") for n in container.get("Names") or []}
    return labels.get("deployra.app") == key or labels.get("com.docker.compose.project") == key or bool(names & {f"app-{key}", f"app-{key}-next"})


def docker_ports(client: Any, owner: str | None = None) -> dict[int, str]:
    """Porty hosta opublikowane przez kontenery → nazwa kontenera (bez kontenerów aplikacji `owner`)."""
    used: dict[int, str] = {}
    for c in client.api.containers(all=True):
        if _owned(c, owner):
            continue
        for p in c.get("Ports") or []:
            if p.get("PublicPort"):
                used.setdefault(int(p["PublicPort"]), (c.get("Names") or ["/?"])[0].lstrip("/"))
    return used


def own_ports(client: Any, key: str) -> set[int]:
    return {int(p["PublicPort"]) for c in client.api.containers(all=True) if _owned(c, key) for p in c.get("Ports") or [] if p.get("PublicPort")}


def holder(state: State, client: Any, key: str, port: int) -> str:
    """Kto trzyma port: kontener, inna aplikacja Deployry albo program spoza Dockera."""
    container = docker_ports(client, key).get(port)
    if container:
        return tr("node.port_by_container", name=container)
    app = next((k for k, p in state.get("ports", {}).items() if k != key and int(p) == port), None)
    return tr("node.port_by_app", name=app.split("#")[0]) if app else tr("node.port_by_host")


def allocate(
    state: State, client: Any, key: str, pool: tuple[int, int], preferred: int | None = None, check_host: bool = True,
    fallback: bool = False,
) -> int:
    """Zwraca port przypisany do `key` (aplikacji); przydziela nowy z puli, jeśli trzeba.

    `check_host=False` dla CT na Proxmoxie — porty CT nie zajmują portów hosta. Port trzymany przez kontener tej
    samej aplikacji jest wolny (stary kontener zaraz zniknie). `fallback` — `preferred` to tylko zapamiętany port
    z poprzedniego wdrożenia: gdy ktoś go zajął, bierzemy wolny z puli zamiast przerywać.
    """
    current = state.get("ports", {})
    if key in current and (preferred is None or current[key] == preferred):
        return int(current[key])
    taken = {int(p) for k, p in current.items() if k != key} | set(docker_ports(client, key))
    mine = own_ports(client, key)

    def free(port: int) -> bool:
        return port not in taken and (port in mine or not check_host or _port_free(port))

    candidates = [preferred] if preferred else []
    if not preferred or fallback:
        candidates += range(pool[0], pool[1] + 1)
    for port in candidates:
        if free(port):
            state.mutate("ports", lambda ports: {**ports, key: port})
            return port
    if preferred and not fallback:
        raise PortTaken(tr("node.port_taken_by", port=preferred, holder=holder(state, client, key, preferred)))
    raise PortTaken(tr("node.no_free_port"))


def release(state: State, key: str) -> None:
    state.mutate("ports", lambda ports: {k: v for k, v in ports.items() if k != key})


# Wdrożenie bez przestojów: port nowej wersji trzymamy pod kluczem `<aplikacja>#next`,
# żeby nie ruszać przydziału działającej wersji, aż bramka przełączy ruch.
SPARE = "#next"


def allocate_spare(state: State, client: Any, key: str, pool: tuple[int, int], check_host: bool = True) -> int:
    """Wolny port z puli dla nowej wersji obok działającej (nigdy ten sam, co stary kontener)."""
    release(state, key + SPARE)  # pozostałość po przerwanym wdrożeniu
    taken = {int(p) for p in state.get("ports", {}).values()} | set(docker_ports(client))
    for port in range(pool[0], pool[1] + 1):
        if port not in taken and (not check_host or _port_free(port)):
            state.mutate("ports", lambda ports: {**ports, key + SPARE: port})
            return port
    raise PortTaken(tr("node.no_free_port"))


def commit_spare(state: State, key: str) -> None:
    """Nowa wersja przejmuje port aplikacji (po przełączeniu bramki)."""
    port = state.get("ports", {}).get(key + SPARE)
    if port is not None:
        state.mutate("ports", lambda ports: {**{k: v for k, v in ports.items() if k != key + SPARE}, key: port})


def release_spare(state: State, key: str) -> None:
    release(state, key + SPARE)
