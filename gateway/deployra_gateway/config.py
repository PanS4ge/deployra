"""Konfiguracja Deployra-WG ze zmiennych środowiskowych."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from deployra_common import wg


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    return default if not value else value.lower() in ("1", "true", "yes", "tak", "on")


def _ipv6_available() -> bool:
    return Path("/proc/net/if_inet6").exists()


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_DATA", "/var/lib/deployra-wg")))
    public_host: str = field(default_factory=lambda: _env("DEPLOYRA_PUBLIC_HOST"))
    wg_port: int = field(default_factory=lambda: int(_env("DEPLOYRA_WG_PORT", "51820")))
    wg_subnet: str = field(default_factory=lambda: str(ipaddress.ip_network(_env("DEPLOYRA_WG_SUBNET", "10.66.0.0/24"))))
    wg_iface: str = field(default_factory=lambda: _env("DEPLOYRA_WG_IFACE", "wg0"))
    wg_enabled: bool = field(default_factory=lambda: _flag("DEPLOYRA_WG", True))
    bootstrap_port: int = field(default_factory=lambda: int(_env("DEPLOYRA_BOOTSTRAP_PORT", "51821")))
    api_port: int = field(default_factory=lambda: int(_env("DEPLOYRA_API_PORT", "2751")))
    api_bind: str = field(default_factory=lambda: _env("DEPLOYRA_API_BIND"))
    nginx_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_NGINX_DIR", "/etc/nginx/deployra")))
    certs_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_CERTS_DIR", "/etc/deployra/certs")))
    acme_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_ACME_DIR", "/var/www/acme")))
    lego_bin: str = field(default_factory=lambda: _env("DEPLOYRA_LEGO", "lego"))
    nginx_reload: bool = field(default_factory=lambda: _flag("DEPLOYRA_NGINX_RELOAD", True))
    ipv6: bool = field(default_factory=lambda: _flag("DEPLOYRA_IPV6", _ipv6_available()))
    reload_timeout: float = field(default_factory=lambda: float(_env("DEPLOYRA_RELOAD_TIMEOUT", "20")))
    analytics_port: int = field(default_factory=lambda: int(_env("DEPLOYRA_ANALYTICS_PORT", "5140")))
    analytics: bool = field(default_factory=lambda: _flag("DEPLOYRA_ANALYTICS", True))
    cloudflared_bin: str = field(default_factory=lambda: _env("DEPLOYRA_CLOUDFLARED", "cloudflared"))

    @property
    def hub_address(self) -> str:
        return wg.hub_address(self.wg_subnet)

    @property
    def hub_endpoint(self) -> str:
        host = self.public_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6
        return f"{host}:{self.wg_port}"

    @property
    def api_url(self) -> str:
        return f"http://{self.hub_address}:{self.api_port}"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.nginx_dir, self.certs_dir, self.acme_dir):
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
