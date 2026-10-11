"""Reguła DOCKER-USER: porty z puli osiągalne tylko przez tunel WG (i z samego hosta)."""

from __future__ import annotations

import shutil
import subprocess

from loguru import logger

from deployra_common.i18n import tr

COMMENT = "deployra-pool"


def _rule(pool: tuple[int, int], iface: str) -> list[str]:
    return [
        "DOCKER-USER",
        "-p", "tcp",
        "-m", "conntrack", "--ctstate", "NEW", "--ctorigdstport", f"{pool[0]}:{pool[1]}",
        "!", "-i", iface,
        "-m", "comment", "--comment", COMMENT,
        "-j", "DROP",
    ]  # fmt: skip


def restrict_pool(pool: tuple[int, int], iface: str = "wg0") -> bool:
    if not shutil.which("iptables"):
        logger.warning(tr("node.log.no_iptables"))
        return False
    rule = _rule(pool, iface)
    check = subprocess.run(["iptables", "-C", *rule], capture_output=True, text=True)
    if check.returncode == 0:
        return True
    result = subprocess.run(["iptables", "-I", *rule], capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(tr("node.log.docker_user_failed", error=result.stderr.strip()))
        return False
    logger.info(tr("node.log.pool_restricted", pool=f"{pool[0]}-{pool[1]}", iface=iface))
    return True
