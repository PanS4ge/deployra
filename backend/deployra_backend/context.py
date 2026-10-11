"""Wspólny kontekst aplikacji: konfiguracja, stan, Docker (endpointy), zadania, podpisy."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from fastapi import Request

from deployra_common.hmac_auth import NonceCache
from deployra_common.jobs import JobRegistry
from deployra_common.server import SignatureGuard

from .config import Config
from .services.native_update import update_agent
from .services.endpoints import EndpointRegistry
from .services.vitals import VitalsSampler
from .state import State


def _docker_from_env() -> Any:
    import docker

    return docker.from_env(timeout=120)


def _docker_socket(socket: str) -> Any:
    import docker

    return docker.DockerClient(base_url=f"unix://{socket}", version="auto", timeout=120)


class Context:
    def __init__(
        self,
        config: Config,
        docker_factory: Callable[[], Any] = _docker_from_env,
        socket_factory: Callable[[str], Any] = _docker_socket,
    ) -> None:
        self.config = config
        config.ensure_dirs()
        self.state = State(config.data_dir / "state.json")
        self.jobs = JobRegistry()
        self.guard = SignatureGuard(self._lookup, NonceCache())
        self.vitals = VitalsSampler()
        self._docker_factory = docker_factory
        self._docker: Any = None
        self._docker_lock = threading.Lock()
        self.endpoints = EndpointRegistry(
            config.proxmox,
            lambda socket: self.docker if socket is None else socket_factory(socket),
            proc=config.proc_dir,
            pve_dir=config.pve_dir,
        )
        self.deploy_locks: dict[str, threading.Lock] = {}
        self.wait_seconds = 6.0  # ile czekać, czy nowy kontener aplikacji wstał
        self.sftp: Any = None
        from .enroll import PairingGate

        self.pair_gate = PairingGate()
        self.updates = update_agent(config)

    def _lookup(self, key_id: str) -> bytes | None:
        return self.state.secret if key_id == "panel" else None

    @property
    def docker(self) -> Any:
        """Docker hosta (endpoint `local`)."""
        with self._docker_lock:
            if self._docker is None:
                self._docker = self._docker_factory()
            return self._docker

    def docker_for(self, endpoint: str | None) -> Any:
        return self.endpoints.client(endpoint)

    def deploy_lock(self, slug: str) -> threading.Lock:
        with self._docker_lock:
            return self.deploy_locks.setdefault(slug, threading.Lock())


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx
