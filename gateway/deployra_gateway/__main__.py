"""Start bramki: `python -m deployra_gateway [reset-pairing]`."""

from __future__ import annotations

import asyncio
import sys

import uvicorn
from loguru import logger

from deployra_common import i18n
from deployra_common.i18n import tr

from . import NAME, __version__, nginx_ctl, nginx_driver
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


def _join_hub(ctx: Context) -> None:
    """Bramka brzegowa: dołączenie do huba (ponawiane — hub albo sieć mogą jeszcze nie odpowiadać)."""
    import time

    if not ctx.config.join_token:
        logger.critical(tr("gw.edge.need_token"))
        sys.exit(2)
    delay = 5
    while True:
        try:
            ctx.edge.join()
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(tr("gw.edge.join_retry", error=exc, delay=delay))
            time.sleep(delay)
            delay = min(delay * 2, 60)


def main() -> None:
    i18n.current.set(i18n.default_language())  # logi w języku DEPLOYRA_LANG
    config = Config()
    ctx = Context(config)

    if len(sys.argv) > 1 and sys.argv[1] == "reset-pairing":
        print(tr("gw.log.new_code", code=ctx.reset_pairing()))
        return

    logger.info(f"{NAME} {__version__}")
    if config.wg_enabled and not config.public_host:
        logger.critical(tr("gw.log.need_public_host"))
        sys.exit(2)

    if nginx_ctl.install_base(config.nginx_dir, config.nginx_bundle):
        logger.info(tr("gw.log.nginx_base_updated"))
    nginx_driver.write_snippets(config.nginx_dir)
    if ctx.edge:
        if not ctx.edge.conf:
            _join_hub(ctx)
        wg_up = ctx.edge.up()
    else:
        wg_up = ctx.hub.up()
    ctx.external.up_all()  # klient zewnętrznego WireGuarda (wg-easy itp.) — przed nginx, żeby cele 10.x były osiągalne
    result = ctx.apply_proxy()
    ctx.banwall.sync(ctx.bans.nets())
    logger.info(tr("gw.log.nginx_config", status=result.status, message=result.message).rstrip())

    if ctx.edge:
        api_host = config.api_bind or (ctx.edge.address if wg_up else ("0.0.0.0" if not config.wg_enabled else "127.0.0.1"))
    else:
        api_host = config.api_bind or (config.hub_address if wg_up else ("0.0.0.0" if not config.wg_enabled else "127.0.0.1"))
    code = ctx.ensure_pairing_code()
    if ctx.edge and code:
        logger.info(tr("gw.edge.waiting_panel", address=ctx.edge.address))
    elif code:
        _banner(
            [
                tr("gw.log.waiting_sync"),
                tr("gw.log.address", address=f"{config.public_host or '<' + tr('node.log.this_host') + '>'}:{config.bootstrap_port}"),
                tr("gw.log.sync_code", code=code),
            ]
        )
    logger.success(tr("gw.log.listening", bootstrap=f"0.0.0.0:{config.bootstrap_port}", api=f"{api_host}:{config.api_port}"))
    asyncio.run(serve(ctx, api_host))


if __name__ == "__main__":
    main()
