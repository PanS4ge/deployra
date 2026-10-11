"""Nadzorca cloudflared: Cloudflare Tunnel jako alternatywne wejście ruchu do nginx bramki.

Panel tworzy tunel przez API Cloudflare i przekazuje tu jego token. Uruchamiamy
`cloudflared tunnel --no-autoupdate run` z tokenem w zmiennej TUNNEL_TOKEN
(nie w argumentach — nie widać go w `ps`) i restartujemy proces po awarii.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import deque
from typing import Any

from loguru import logger

from deployra_common.i18n import tr


class Cloudflared:
    def __init__(self, state: Any, binary: str = "cloudflared") -> None:
        self.state = state
        self.binary = binary
        self.lines: deque[str] = deque(maxlen=60)
        self.proc: asyncio.subprocess.Process | None = None
        self.started: float | None = None
        self._changed = asyncio.Event()

    @property
    def token(self) -> str | None:
        return self.state.get("cloudflared_token")

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def set_token(self, token: str | None) -> None:
        self.state.update(cloudflared_token=token or None)
        self._changed.set()

    def status(self) -> dict[str, Any]:
        running = self.proc is not None and self.proc.returncode is None
        return {
            "enabled": bool(self.token),
            "available": self.available(),
            "running": running,
            "uptime": int(time.time() - self.started) if running and self.started else None,
            "log": list(self.lines)[-12:],
        }

    async def _stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            self.lines.append(raw.decode(errors="replace").rstrip())

    async def run(self) -> None:
        backoff = 5
        while True:
            self._changed.clear()
            token = self.token
            if not token or not self.available():
                if token and not self.available():
                    logger.error(tr("gw.log.no_cloudflared"))
                await self._changed.wait()
                continue
            env = {**os.environ, "TUNNEL_TOKEN": token}
            self.proc = await asyncio.create_subprocess_exec(
                self.binary, "tunnel", "--no-autoupdate", "run",
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            self.started = time.time()
            logger.info(tr("gw.log.cloudflared_started"))
            pump = asyncio.create_task(self._pump(self.proc))
            waiter = asyncio.create_task(self.proc.wait())
            changed = asyncio.create_task(self._changed.wait())
            done, _ = await asyncio.wait({waiter, changed}, return_when=asyncio.FIRST_COMPLETED)
            if changed in done:
                await self._stop()
                backoff = 5
            else:
                changed.cancel()
                code = self.proc.returncode
                lived = time.time() - (self.started or time.time())
                logger.warning(tr("gw.log.cloudflared_exited", code=code, delay=backoff))
                backoff = 5 if lived > 300 else min(backoff * 2, 120)
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
            pump.cancel()
