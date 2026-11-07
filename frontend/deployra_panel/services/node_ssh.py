"""WebSSH do węzła: panel łączy się po SSH bezpośrednio (szyfrowanie od końca do końca, także do węzłów bez tunelu).

Klucz hosta jest zaufany dopiero po akceptacji (TOFU): najpierw samo pobranie klucza (bez logowania), panel pokazuje odcisk
SHA256, administrator go potwierdza. Połączenie z hasłem/kluczem idzie potem z tym kluczem jako jedynym zaufanym —
podstawiony serwer (MITM) nie dostanie hasła. Zmiana klucza = odmowa, dopóki ktoś świadomie nie zaakceptuje nowego.

Hasło, klucz prywatny i hasło klucza są zaszyfrowane `Sealer` (AES-GCM) i nigdy nie wracają do przeglądarki.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..i18n import t

HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.IGNORECASE)
REAUTH_SECONDS = 15 * 60
IDLE_SECONDS = 30 * 60


class HostKeyError(Exception):
    """Klucz hosta nieznany albo inny niż zaufany — `fingerprint` i `key` do pokazania/akceptacji."""

    def __init__(self, message: str, fingerprint: str, key: str, changed: bool) -> None:
        super().__init__(message)
        self.fingerprint, self.key, self.changed = fingerprint, key, changed


def default_host(panel: Any, node: dict) -> str:
    if node.get("transport") == "tunnel" and node.get("tunnel_ip"):
        return node["tunnel_ip"]
    if node.get("proxy_address") == "local":
        return "127.0.0.1"  # węzeł obok panelu: panel działa w sieci hosta (stack panel-node)
    return urlsplit(str(node.get("api_url") or "")).hostname or ""


def settings(node: dict) -> dict:
    """Ustawienia SSH bez sekretów (do formularza i do decyzji, czy przycisk WebSSH jest aktywny)."""
    raw = node.get("ssh") or {}
    return {"host": raw.get("host") or "", "port": int(raw.get("port") or 22), "username": raw.get("username") or "",
            "auth": raw.get("auth") or "password", "has_secret": bool(raw.get("secret")), "fingerprint": raw.get("fingerprint") or ""}  # fmt: skip


def configured(node: dict) -> bool:
    s = settings(node)
    return bool(s["username"] and s["has_secret"])


def save(panel: Any, node: dict, form: Any) -> dict:
    raw = dict(node.get("ssh") or {})
    host = str(form.get("host") or "").strip()
    username = str(form.get("username") or "").strip()
    try:
        port = int(form.get("port") or 22)
    except ValueError:
        port = 0
    if host and not HOST.match(host):
        raise ValueError(t("ssh.bad_host"))
    if username and not USER.match(username):
        raise ValueError(t("ssh.bad_user"))
    if not 1 <= port <= 65535:
        raise ValueError(t("ssh.bad_port"))
    auth = "key" if form.get("auth") == "key" else "password"
    if host != raw.get("host") or port != int(raw.get("port") or 22):  # inny serwer = klucz hosta do ponownej akceptacji
        raw.pop("host_key", None)
        raw.pop("fingerprint", None)
    raw.update(host=host, port=port, username=username, auth=auth)
    secret = str(form.get("key") if auth == "key" else form.get("password") or "")
    if auth == "key" and secret.strip():
        import asyncssh

        try:
            asyncssh.import_private_key(secret.strip(), str(form.get("passphrase") or "") or None)
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise ValueError(t("ssh.bad_key", error=exc)) from None
        raw["secret"] = panel.sealer.seal(secret.strip())
        raw["passphrase"] = panel.sealer.seal(str(form.get("passphrase") or "")) if form.get("passphrase") else None
    elif auth == "password" and secret:
        raw["secret"] = panel.sealer.seal(secret)
        raw["passphrase"] = None
    if form.get("clear"):
        raw = {}
    panel.db.update("nodes", node["id"], ssh=raw or None)
    return raw


def _target(panel: Any, node: dict) -> tuple[str, int]:
    s = settings(node)
    return (s["host"] or default_host(panel, node)).strip("[]"), s["port"]


async def fetch_host_key(panel: Any, node: dict) -> tuple[str, str]:
    """(odcisk SHA256, klucz w formacie OpenSSH) — bez logowania; do pokazania i akceptacji."""
    import asyncssh

    host, port = _target(panel, node)
    import asyncio

    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port, config=None), 15)  # config=None: bez ~/.ssh/config panelu
    if key is None:
        raise ConnectionError(t("ssh.no_key"))
    return key.get_fingerprint("sha256"), key.export_public_key("openssh").decode().strip()


def accept_key(panel: Any, node: dict, fingerprint: str, key: str) -> None:
    raw = dict(node.get("ssh") or {})
    raw.update(host_key=key, fingerprint=fingerprint)
    panel.db.update("nodes", node["id"], ssh=raw)


async def connect(panel: Any, node: dict):
    """Połączenie z zaufanym kluczem hosta (sprawdzanym przed uwierzytelnieniem). Bez zaufanego klucza — HostKeyError."""
    import asyncssh

    raw = node.get("ssh") or {}
    host, port = _target(panel, node)
    if not raw.get("host_key"):
        fingerprint, key = await fetch_host_key(panel, node)
        raise HostKeyError(t("ssh.key_unknown", fingerprint=fingerprint), fingerprint, key, changed=False)
    entry = f"[{host}]:{port}" if port != 22 else host
    known = asyncssh.import_known_hosts(f"{entry} {raw['host_key']}\n")
    options: dict[str, Any] = {"username": raw.get("username"), "known_hosts": known, "connect_timeout": 15, "login_timeout": 20,
                               "keepalive_interval": 30, "config": None}  # fmt: skip
    secret = panel.sealer.unseal(raw.get("secret"))
    if raw.get("auth") == "key":
        passphrase = panel.sealer.unseal(raw.get("passphrase")) or None
        options.update(client_keys=[asyncssh.import_private_key(secret, passphrase)], password=None, agent_path=None)
    else:
        options.update(password=secret, client_keys=None, agent_path=None)
    try:
        return await asyncssh.connect(host, port, **options)
    except asyncssh.HostKeyNotVerifiable:
        fingerprint, key = await fetch_host_key(panel, node)
        raise HostKeyError(t("ssh.key_changed", fingerprint=fingerprint, trusted=raw.get("fingerprint") or "?"), fingerprint, key,
                           changed=True) from None  # fmt: skip
