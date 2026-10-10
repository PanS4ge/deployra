"""Reguła DOCKER-USER: porty z puli osiągalne tylko przez tunel WG (i z samego hosta)."""

from __future__ import annotations

import shutil
import subprocess

from loguru import logger

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
        logger.warning("Brak iptables — pula portów nie jest ograniczona do tunelu")
        return False
    rule = _rule(pool, iface)
    check = subprocess.run(["iptables", "-C", *rule], capture_output=True, text=True)
    if check.returncode == 0:
        return True
    result = subprocess.run(["iptables", "-I", *rule], capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"Nie udało się dodać reguły DOCKER-USER: {result.stderr.strip()}")
        return False
    logger.info(f"Pula portów {pool[0]}-{pool[1]} dostępna tylko przez {iface}")
    return True
