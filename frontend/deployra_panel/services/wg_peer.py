"""WireGuard panelu — panel jest zwykłym peerem huba i rozmawia z węzłami przez tunel."""

from __future__ import annotations

import ipaddress
import subprocess
import sys
from typing import Any

from loguru import logger

from deployra_common import wg


def available(panel: Any) -> bool:
    return panel.config.wg_mode != "off" and sys.platform.startswith("linux") and wg.WgControl.tools_available()


def build_config(gateway: dict[str, Any], private_key: str) -> str:
    prefix = ipaddress.ip_network(gateway["subnet"]).prefixlen
    iface = wg.WgInterface(private_key=private_key, addresses=[f"{gateway['panel_address']}/{prefix}"])
    hub = wg.WgPeer(
        public_key=gateway["hub_public_key"],
        allowed_ips=[gateway["subnet"]],
        endpoint=f"{gateway['public_host']}:{gateway['wg_port']}" if ":" not in gateway["public_host"] else f"[{gateway['public_host']}]:{gateway['wg_port']}",
        keepalive=25,
        comment="Deployra-WG",
    )
    return wg.render_quick(iface, [hub])


def hub_is_local(hub_address: str, iface: str = "wg0") -> bool:
    """Czy panel stoi na tym samym hoście co bramka (wg0 ma adres huba)."""
    try:
        out = subprocess.run(["ip", "-o", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f" {hub_address}/" in out


def bring_up(panel: Any, gateway: dict[str, Any]) -> bool:
    private = panel.sealer.unseal(gateway.get("panel_wg_private"))
    if not private or not gateway.get("panel_address"):
        return False
    control = wg.WgControl(panel.config.wg_iface)
    control.write_conf(build_config(gateway, private))
    try:
        control.up()
    except RuntimeError as exc:
        logger.error(f"Tunel panelu nie wstał: {exc}")
        return False
    logger.success(f"Tunel panelu działa ({gateway['panel_address']})")
    return True


def ensure_up(panel: Any) -> None:
    gateway = panel.gateway()
    if not gateway or not gateway.get("panel_wg_private") or not available(panel):
        return
    if wg.WgControl(panel.config.wg_iface).is_up():
        return
    bring_up(panel, gateway)
