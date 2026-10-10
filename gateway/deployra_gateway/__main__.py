"""Start bramki: `python -m deployra_gateway [reset-pairing]`."""

from __future__ import annotations

import asyncio
import sys

import uvicorn
from loguru import logger

from . import NAME, __version__, nginx_driver
from .app import create_api_app, create_bootstrap_app
from .config import Config
from .context import Context


def _banner(lines: list[str]) -> None:
    width = max(len(line) for line in lines) + 4
    logger.opt(colors=True).info("<magenta>" + "═" * width + "</magenta>")
    for line in lines:
        safe = line.replace("<", r"\<")  # treść może zawierać <…> — loguru wziąłby to za tag koloru
        logger.opt(colors=True).info(f"<magenta>║</magenta> <bold>{safe}</bold>")
    logger.opt(colors=True).info("<magenta>" + "═" * width + "</magenta>")


async def serve(ctx: Context, api_host: str) -> None:
    config = ctx.config
    servers = [
        uvicorn.Server(
            uvicorn.Config(create_bootstrap_app(ctx), host="0.0.0.0", port=config.bootstrap_port, log_level="warning", server_header=False)
        ),
        uvicorn.Server(
            uvicorn.Config(create_api_app(ctx), host=api_host, port=config.api_port, log_level="warning", server_header=False)
        ),
    ]
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    config = Config()
    ctx = Context(config)

    if len(sys.argv) > 1 and sys.argv[1] == "reset-pairing":
        print(f"Nowy kod synchronizacji: {ctx.reset_pairing()}")
        return

    logger.info(f"{NAME} {__version__}")
    if config.wg_enabled and not config.public_host:
        logger.critical("Ustaw DEPLOYRA_PUBLIC_HOST (publiczny adres/IP tego VPS) — węzły muszą wiedzieć, dokąd się łączyć")
        sys.exit(2)

    nginx_driver.write_snippets(config.nginx_dir)
    wg_up = ctx.hub.up()
    result = ctx.apply_proxy()
    logger.info(f"Konfiguracja nginx: {result.status} {result.message}".rstrip())

    api_host = config.api_bind or (config.hub_address if wg_up else "127.0.0.1")
    code = ctx.ensure_pairing_code()
    if code:
        _banner(
            [
                "Deployra-WG czeka na synchronizację z panelem Deployry.",
                f"Adres bramki: {config.public_host or '<ten-host>'}:{config.bootstrap_port}",
                f"Kod synchronizacji: {code}",
            ]
        )
    logger.success(f"Bootstrap: 0.0.0.0:{config.bootstrap_port} • API panelu: {api_host}:{config.api_port}")
    asyncio.run(serve(ctx, api_host))


if __name__ == "__main__":
    main()
