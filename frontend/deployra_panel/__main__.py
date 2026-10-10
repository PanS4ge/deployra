"""Start panelu: `python -m deployra_panel`."""

from __future__ import annotations

import uvicorn
from loguru import logger

from . import NAME, __version__
from .app import create_app
from .config import Config


def main() -> None:
    config = Config()
    logger.info(f"{NAME} {__version__} — panel na http://{config.host}:{config.port} (dane: {config.data_dir})")
    app = create_app(config)
    if config.open_browser:
        import threading
        import webbrowser

        host = "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host
        threading.Timer(1.5, webbrowser.open, [f"http://{host}:{config.port}"]).start()
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips=config.trusted_proxies,
        server_header=False,
        ws_max_size=1 << 20,
    )


if __name__ == "__main__":
    main()
