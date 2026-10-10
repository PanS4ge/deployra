"""WireGuard: klucze, konfiguracje i sterowanie interfejsem.

Klucze generuje `cryptography` (X25519 — bajtowo zgodne z `wg genkey`/`wg pubkey`).
Peery zmieniamy przez `wg syncconf`, które nie zrywa istniejących sesji.
Gdy kernel nie ma WireGuarda (OpenVZ/LXC), `wg-quick` sam uruchamia wireguard-go.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def genkey() -> tuple[str, str]:
    """(prywatny, publiczny) w formacie `wg genkey` / `wg pubkey`."""
    key = X25519PrivateKey.generate()
    return (
        base64.b64encode(key.private_bytes_raw()).decode(),
        base64.b64encode(key.public_key().public_bytes_raw()).decode(),
    )


def pubkey(private_b64: str) -> str:
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(private_b64, validate=True))
    return base64.b64encode(key.public_key().public_bytes_raw()).decode()


def genpsk() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def valid_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def _check_cidr(value: str) -> str:
    return str(ipaddress.ip_interface(value.strip()) if "/" in value else ipaddress.ip_address(value.strip()))


def _check_network(value: str) -> str:
    return str(ipaddress.ip_network(value.strip(), strict=False))


_ENDPOINT_HOST = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)$")


def _check_endpoint(value: str) -> str:
    host, sep, port = value.strip().rpartition(":")
    if not sep or not _ENDPOINT_HOST.match(host) or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"zły endpoint: {value!r}")
    return f"{host}:{port}"


@dataclass
class WgPeer:
    public_key: str
    allowed_ips: list[str]
    endpoint: str | None = None
    keepalive: int | None = None
    preshared_key: str | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        if not valid_key(self.public_key):
            raise ValueError("zły klucz publiczny peera")
        if self.preshared_key and not valid_key(self.preshared_key):
            raise ValueError("zły klucz PSK")
        self.allowed_ips = [_check_network(ip) for ip in self.allowed_ips]
        if self.endpoint:
            self.endpoint = _check_endpoint(self.endpoint)
        if self.keepalive is not None and not 0 <= int(self.keepalive) <= 65535:
            raise ValueError("zły PersistentKeepalive")
        if self.comment:
            self.comment = " ".join(self.comment.split())[:80]


@dataclass
class WgInterface:
    private_key: str
    addresses: list[str]
    listen_port: int | None = None
    mtu: int | None = None
    post_up: list[str] = field(default_factory=list)
    post_down: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not valid_key(self.private_key):
            raise ValueError("zły klucz prywatny interfejsu")
        self.addresses = [_check_cidr(a) for a in self.addresses]
        for cmd in self.post_up + self.post_down:
            if "\n" in cmd or "\r" in cmd:
                raise ValueError("polecenia PostUp/PostDown muszą być jednoliniowe")


def _peer_block(peer: WgPeer) -> list[str]:
    lines = ["", "[Peer]"]
    if peer.comment:
        lines.insert(1, f"# {peer.comment}")
    lines.append(f"PublicKey = {peer.public_key}")
    if peer.preshared_key:
        lines.append(f"PresharedKey = {peer.preshared_key}")
    lines.append(f"AllowedIPs = {', '.join(peer.allowed_ips)}")
    if peer.endpoint:
        lines.append(f"Endpoint = {peer.endpoint}")
    if peer.keepalive:
        lines.append(f"PersistentKeepalive = {peer.keepalive}")
    return lines


def render_quick(iface: WgInterface, peers: Iterable[WgPeer]) -> str:
    """Konfiguracja w formacie wg-quick (z Address, PostUp…)."""
    lines = ["# Wygenerowane przez Deployrę — nie edytuj ręcznie", "[Interface]", f"PrivateKey = {iface.private_key}"]
    lines.append(f"Address = {', '.join(iface.addresses)}")
    if iface.listen_port:
        lines.append(f"ListenPort = {iface.listen_port}")
    if iface.mtu:
        lines.append(f"MTU = {iface.mtu}")
    lines += [f"PostUp = {c}" for c in iface.post_up]
    lines += [f"PostDown = {c}" for c in iface.post_down]
    for peer in peers:
        lines += _peer_block(peer)
    return "\n".join(lines) + "\n"


def render_sync(iface: WgInterface, peers: Iterable[WgPeer]) -> str:
    """Konfiguracja w formacie wg(8) dla `wg syncconf` (bez pól wg-quick)."""
    lines = ["[Interface]", f"PrivateKey = {iface.private_key}"]
    if iface.listen_port:
        lines.append(f"ListenPort = {iface.listen_port}")
    for peer in peers:
        lines += _peer_block(peer)
    return "\n".join(lines) + "\n"


def allocate_ip(subnet: str, used: Iterable[str], skip_first: int = 1) -> str:
    """Pierwszy wolny adres hosta w podsieci (pierwszy adres należy do huba)."""
    net = ipaddress.ip_network(subnet, strict=False)
    taken = {str(ipaddress.ip_interface(u).ip) if "/" in u else u for u in used}
    for index, host in enumerate(net.hosts()):
        if index < skip_first:
            continue
        if str(host) not in taken:
            return str(host)
    raise RuntimeError(f"brak wolnych adresów w {subnet}")


def hub_address(subnet: str) -> str:
    return str(next(ipaddress.ip_network(subnet, strict=False).hosts()))


def parse_dump(text: str) -> list[dict]:
    """Parsuje `wg show <if> dump` (pierwsza linia to interfejs, dalej peery)."""
    peers = []
    for line in text.strip().splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 8:
            continue
        pub, _psk, endpoint, allowed, handshake, rx, tx, keepalive = cols[:8]
        peers.append(
            {
                "public_key": pub,
                "endpoint": None if endpoint == "(none)" else endpoint,
                "allowed_ips": [] if allowed == "(none)" else allowed.split(","),
                "latest_handshake": int(handshake or 0),
                "rx_bytes": int(rx or 0),
                "tx_bytes": int(tx or 0),
                "keepalive": None if keepalive in ("off", "") else int(keepalive),
            }
        )
    return peers


Runner = Callable[..., subprocess.CompletedProcess]


class WgControl:
    """Sterowanie interfejsem przez `wg` / `wg-quick` / `ip`."""

    def __init__(self, iface: str = "wg0", conf_dir: str = "/etc/wireguard", runner: Runner = subprocess.run) -> None:
        self.iface = iface
        self.conf_dir = Path(conf_dir)
        self._run = runner

    @property
    def conf_path(self) -> Path:
        return self.conf_dir / f"{self.iface}.conf"

    @staticmethod
    def tools_available() -> bool:
        return shutil.which("wg") is not None and shutil.which("wg-quick") is not None

    def _exec(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        return self._run(list(args), input=input_text, capture_output=True, text=True, timeout=30, check=False)

    def write_conf(self, text: str) -> Path:
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.conf_dir, prefix=f".{self.iface}.")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.conf_path)
        return self.conf_path

    def is_up(self) -> bool:
        return self._exec("wg", "show", self.iface).returncode == 0

    def up(self) -> None:
        if self.is_up():
            self._exec("wg-quick", "down", str(self.conf_path))
        result = self._exec("wg-quick", "up", str(self.conf_path))
        if result.returncode != 0:
            raise RuntimeError(f"wg-quick up nie powiodło się: {result.stderr.strip()}")

    def down(self) -> None:
        if self.is_up():
            self._exec("wg-quick", "down", str(self.conf_path))

    def syncconf(self, sync_text: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix="deployra-wg-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(sync_text)
            result = self._exec("wg", "syncconf", self.iface, tmp)
            if result.returncode != 0:
                raise RuntimeError(f"wg syncconf nie powiodło się: {result.stderr.strip()}")
        finally:
            os.unlink(tmp)

    def add_route(self, network: str) -> None:
        """Trasa przez interfejs (wg syncconf nie dodaje tras dla nowych AllowedIPs)."""
        self._exec("ip", "route", "replace", _check_network(network), "dev", self.iface)

    def dump(self) -> list[dict]:
        result = self._exec("wg", "show", self.iface, "dump")
        return parse_dump(result.stdout) if result.returncode == 0 else []
