"""Start backendu: `python -m deployra_backend [reset-pairing]`."""

from __future__ import annotations

import sys

import uvicorn
from loguru import logger

from . import NAME, __version__, enroll
from .app import create_app
from .config import Config
from .context import Context
from .services import firewall, wg_spoke


def _banner(lines: list[str]) -> None:
    width = max(len(line) for line in lines) + 4
    logger.opt(colors=True).info("<magenta>" + "═" * width + "</magenta>")
    for line in lines:
        logger.opt(colors=True).info(f"<magenta>║</magenta> <bold>{line}</bold>")
    logger.opt(colors=True).info("<magenta>" + "═" * width + "</magenta>")


def main() -> None:
    config = Config()
    ctx = Context(config)

    if len(sys.argv) > 1 and sys.argv[1] == "reset-pairing":
        code = enroll.reset_pairing(ctx)
        print(f"Nowy kod parowania: {code}")
        return

    logger.info(f"{NAME} {__version__} — węzeł {config.name} ({ctx.state.get('node_id')})")

    if not ctx.state.paired and config.join_token and not ctx.state.get("wg"):
        if not config.gateway:
            logger.critical("DEPLOYRA_JOIN wymaga DEPLOYRA_GATEWAY (adres bramki, np. vps.example.com:51821)")
            sys.exit(2)
        enroll.join_with_retry(ctx)

    bind = config.api_bind
    wg_conf = ctx.state.get("wg")
    if wg_conf and config.wg_enabled:
        if wg_spoke.bring_up(wg_conf, config.wg_iface):
            bind = bind or wg_conf["address"]
            if config.restrict_pool:
                firewall.restrict_pool(config.port_pool, config.wg_iface)
        else:
            logger.warning("Tunel nie wstał — API słucha na wszystkich interfejsach (chronione podpisem)")
    bind = bind or "0.0.0.0"

    code = enroll.ensure_pairing_code(ctx)
    if code:
        _banner(
            [
                "Węzeł czeka na sparowanie z panelem Deployry.",
                f"Adres: http://<ten-host>:{config.api_port}",
                f"Kod parowania: {code}",
            ]
        )
    elif ctx.state.get("join"):
        logger.info("Węzeł dołączył do bramki i czeka, aż panel dokończy parowanie przez tunel")

    app = create_app(ctx, bind_host=bind)
    logger.success(f"API słucha na {bind}:{config.api_port}")
    uvicorn.run(app, host=bind, port=config.api_port, log_level="warning", proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
