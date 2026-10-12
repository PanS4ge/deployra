"""Start backendu: `python -m deployra_backend [reset-pairing]`."""

from __future__ import annotations

import sys

import uvicorn
from loguru import logger

from deployra_common import i18n
from deployra_common.i18n import tr

from . import NAME, __version__, enroll
from .app import create_app
from .config import Config
from .context import Context
from .services import firewall, wg_spoke


def _banner(lines: list[str]) -> None:
    width = max(len(line) for line in lines) + 4
    logger.opt(colors=True).info("<magenta>" + "═" * width + "</magenta>")
    for line in lines:
        safe = line.replace("<", r"\<")  # treść może zawierać <…> — loguru wziąłby to za tag koloru
        logger.opt(colors=True).info(f"<magenta>║</magenta> <bold>{safe}</bold>")
    logger.opt(colors=True).info("<magenta>" + "═" * width + "</magenta>")


def main() -> None:
    i18n.current.set(i18n.default_language())  # logi w języku DEPLOYRA_LANG
    config = Config()
    if config.fake_docker:
        from .fake_docker import FakeDocker

        logger.warning(tr("node.log.fake_docker"))
        ctx = Context(config, docker_factory=lambda: FakeDocker(demo=True))
    else:
        ctx = Context(config)

    if len(sys.argv) > 1 and sys.argv[1] == "reset-pairing":
        code = enroll.reset_pairing(ctx)
        print(tr("node.log.new_code", code=code))
        return

    logger.info(f"{NAME} {__version__} — {tr('node.log.node', name=config.name)} ({ctx.state.get('node_id')})")

    if not ctx.state.paired and config.join_token and not ctx.state.get("wg"):
        if not config.gateway:
            logger.critical(tr("node.log.join_needs_gateway"))
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
            logger.warning(tr("node.log.tunnel_down"))
    bind = bind or "0.0.0.0"
    if not wg_conf and config.restrict_pool and bind in ("127.0.0.1", "::1", "localhost"):
        # węzeł obok bramki (all-in-one): bramka łączy się po 127.0.0.1, z zewnątrz pula tylko przez tunel —
        # porty publikowane przez Dockera omijają ufw, więc bez tej reguły aplikacje byłyby widoczne z internetu
        firewall.restrict_pool(config.port_pool, config.wg_iface)

    code = enroll.ensure_pairing_code(ctx)
    if code:
        _banner(
            [
                tr("node.log.waiting_pair"),
                tr("node.log.address", url=f"http://<{tr('node.log.this_host')}>:{config.api_port}"),
                tr("node.log.pairing_code", code=code),
            ]
        )
    elif ctx.state.get("join"):
        logger.info(tr("node.log.joined_waiting"))

    app = create_app(ctx, bind_host=bind)
    logger.success(tr("node.log.api_listening", address=f"{bind}:{config.api_port}"))
    uvicorn.run(app, host=bind, port=config.api_port, log_level="warning", proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
