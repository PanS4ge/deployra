"""Konfiguracja panelu ze zmiennych środowiskowych."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    return default if not value else value.lower() in ("1", "true", "yes", "tak", "on")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_DATA", "./data")).resolve())
    host: str = field(default_factory=lambda: _env("DEPLOYRA_BIND", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("DEPLOYRA_PORT", "8080")))
    db_url: str = field(default_factory=lambda: _env("DEPLOYRA_DB", ""))
    wg_mode: str = field(default_factory=lambda: _env("DEPLOYRA_WG", "auto"))  # auto | off
    wg_iface: str = field(default_factory=lambda: _env("DEPLOYRA_WG_IFACE", "wg0"))
    trusted_proxies: str = field(default_factory=lambda: _env("DEPLOYRA_TRUSTED_PROXIES", "127.0.0.1,10.66.0.1"))
    secure_cookies: bool = field(default_factory=lambda: _flag("DEPLOYRA_SECURE_COOKIES", False))
    session_hours: int = field(default_factory=lambda: int(_env("DEPLOYRA_SESSION_HOURS", "24")))
    open_browser: bool = field(default_factory=lambda: _flag("DEPLOYRA_OPEN_BROWSER", False))
    monitoring: bool = field(default_factory=lambda: _flag("DEPLOYRA_MONITORING", True))
    language: str = field(default_factory=lambda: _env("DEPLOYRA_LANG"))  # domyślny język, gdy nie ustawiono w panelu
    update_repo: str = field(default_factory=lambda: _env("DEPLOYRA_UPDATE_REPO", "PanS4ge/deployra"))  # owner/repo z wydaniami albo URL JSON
    update_dir: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_UPDATE_DIR", "/run/deployra-update")))
    update_checks: bool = field(default_factory=lambda: _flag("DEPLOYRA_UPDATE_CHECKS", True))  # pętla sprawdzania wydań
    vuln_checks: bool = field(default_factory=lambda: _flag("DEPLOYRA_VULN_CHECKS", True))  # pętla skanowania podatności (OSV.dev)
    image_checks: bool = field(default_factory=lambda: _flag("DEPLOYRA_IMAGE_CHECKS", True))  # pętla aktualizacji obrazów aplikacji
    plugins_dir_env: str = field(default_factory=lambda: _env("DEPLOYRA_PLUGINS_DIR"))
    # węzeł na tej samej maszynie (deploy/panel-node.compose.yml): panel paruje go sam kodem ze wspólnego wolumenu
    local_node: str = field(default_factory=lambda: _env("DEPLOYRA_LOCAL_NODE"))
    local_node_name: str = field(default_factory=lambda: _env("DEPLOYRA_LOCAL_NODE_NAME", "dom"))
    local_node_code: Path = field(default_factory=lambda: Path(_env("DEPLOYRA_LOCAL_NODE_CODE", "/run/deployra-pair/code")))

    @property
    def plugins_dir(self) -> Path:
        return Path(self.plugins_dir_env) if self.plugins_dir_env else self.data_dir / "plugins"

    def __post_init__(self) -> None:
        if not self.db_url:
            self.db_url = f"sqlite:///{(self.data_dir / 'deployra.sqlite').as_posix()}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
