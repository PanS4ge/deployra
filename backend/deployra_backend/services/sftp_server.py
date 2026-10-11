"""Serwer SFTP w procesie backendu (asyncssh).

Konta są w stanie węzła i działają od razu (bez restartu). Każde konto jest
zamknięte (chroot) w katalogu swojej strony. Tylko SFTP — bez powłoki,
exec i przekierowań portów.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import asyncssh
from loguru import logger

from deployra_common.i18n import tr
from deployra_common.passwords import verify_password
from deployra_common.schemas import check_slug

class _Server(asyncssh.SSHServer):
    def __init__(self, service: "SftpService") -> None:
        self.service = service

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        user = self.service.user(username)
        return bool(user and verify_password(password, user.get("hash")))

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        user = self.service.user(username)
        if not user:
            return False
        wanted = key.export_public_key("openssh").split()[1]
        return any(k.split()[1] == wanted for k in user.get("keys", []) if len(k.split()) >= 2)


class SftpService:
    def __init__(self, state: Any, sites_dir: Path, key_path: Path) -> None:
        self.state = state
        self.sites_dir = sites_dir
        self.key_path = key_path
        self._acceptor: Any = None

    def user(self, username: str) -> dict | None:
        return self.state.get("sftp_users", {}).get(username)

    def _host_key(self) -> asyncssh.SSHKey:
        if not self.key_path.exists():
            key = asyncssh.generate_private_key("ssh-ed25519")
            key.write_private_key(str(self.key_path))
            os.chmod(self.key_path, 0o600)
        return asyncssh.read_private_key(str(self.key_path))

    def _sftp_factory(self, chan: asyncssh.SSHServerChannel) -> asyncssh.SFTPServer:
        user = self.user(chan.get_extra_info("username")) or {}
        root = self.sites_dir / check_slug(user.get("slug", "x"))
        root.mkdir(parents=True, exist_ok=True)
        return asyncssh.SFTPServer(chan, chroot=os.fsencode(root))

    async def start(self, host: str, port: int) -> None:
        self._acceptor = await asyncssh.create_server(
            lambda: _Server(self),
            host,
            port,
            server_host_keys=[self._host_key()],
            sftp_factory=self._sftp_factory,
            allow_scp=False,
            agent_forwarding=False,
            x11_forwarding=False,
            server_version="Deployra",
        )
        logger.info(tr("node.log.sftp_listening", address=f"{host or '*'}:{port}"))

    async def stop(self) -> None:
        if self._acceptor:
            self._acceptor.close()
            await self._acceptor.wait_closed()

    def fingerprint(self) -> str | None:
        if not self.key_path.exists():
            return None
        return asyncssh.read_private_key(str(self.key_path)).get_fingerprint()
