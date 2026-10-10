"""Konfiguracja backendu ze zmiennych środowiskowych."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "tak", "on")


def _pool(value: str) -> tuple[int, int]:
    low, _, high = value.partition("-")
    lo, hi = int(low), int(high or low)
    if not 1024 <= lo <= hi <= 65535:
        raise ValueError("DEPLOYRA_PORT_POOL: zakres np. 20000-29999")
    return lo, hi


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_DATA", "/var/lib/deployra")))
    srv_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_SRV", "/srv/deployra")))
    name: str = field(default_factory=lambda: _env("DEPLOYRA_NAME") or socket.gethostname())
    api_port: int = field(default_factory=lambda: int(_env("DEPLOYRA_API_PORT", "2750")))
    api_bind: str = field(default_factory=lambda: _env("DEPLOYRA_API_BIND"))
    gateway: str = field(default_factory=lambda: _env("DEPLOYRA_GATEWAY"))
    join_token: str = field(default_factory=lambda: _env("DEPLOYRA_JOIN"))
    wg_enabled: bool = field(default_factory=lambda: _flag("DEPLOYRA_WG", True))
    wg_iface: str = field(default_factory=lambda: _env("DEPLOYRA_WG_IFACE", "wg0"))
    port_pool: tuple[int, int] = field(default_factory=lambda: _pool(_env("DEPLOYRA_PORT_POOL", "20000-29999")))
    restrict_pool: bool = field(default_factory=lambda: _flag("DEPLOYRA_RESTRICT_POOL", True))
    sftp_port: int = field(default_factory=lambda: int(_env("DEPLOYRA_SFTP_PORT", "2222")))
    sftp_enabled: bool = field(default_factory=lambda: _flag("DEPLOYRA_SFTP", True))
    docker_network: str = field(default_factory=lambda: _env("DEPLOYRA_DOCKER_NETWORK", "deployra"))
    static_image: str = field(default_factory=lambda: _env("DEPLOYRA_STATIC_IMAGE", "nginx:stable-alpine"))
    max_upload_mb: int = field(default_factory=lambda: int(_env("DEPLOYRA_MAX_UPLOAD_MB", "512")))

    @property
    def sites_dir(self) -> Path:
        return self.srv_dir / "sites"

    @property
    def stacks_dir(self) -> Path:
        return self.srv_dir / "stacks"

    @property
    def builds_dir(self) -> Path:
        return self.srv_dir / "builds"

    @property
    def static_conf_dir(self) -> Path:
        return self.srv_dir / "static-nginx"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.sites_dir, self.stacks_dir, self.builds_dir, self.static_conf_dir):
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
