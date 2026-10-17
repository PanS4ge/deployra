"""Serwery Minecraft przez plugin DeployraBridge.

Plugin sam łączy się z panelem (serwer Minecraft nie otwiera żadnego portu): paruje się jednorazowym kodem,
a potem trzyma długie zapytanie `POST /minecraft/bridge/poll`. Panel oddaje w nim kolejkę poleceń, plugin
odsyła wyniki w następnym zapytaniu. Żądania pluginu i odpowiedzi panelu są podpisane HMAC (jak między
komponentami Deployry: ts, nonce, skrót body) — podrobione albo powtórzone polecenie zostanie odrzucone.
"""

from __future__ import annotations

import asyncio
import ftplib
import hashlib
import io
import json
import posixpath
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from deployra_common import hmac_auth

PAIR_TTL = 24 * 3600
POLL_SECONDS = 25
ONLINE_SECONDS = 45
WINDOW = 300  # zegary hostingów bywają rozjechane o minutę-dwie
FILE_LIMIT = 8 * 1024 * 1024
PLAYER = re.compile(r"^[A-Za-z0-9_]{1,16}$")
PLAYER_ACTIONS = ("survival", "creative", "adventure", "spectator", "heal", "feed", "kill", "starve", "op", "deop",
                  "whitelist_add", "whitelist_remove", "kick", "ban", "pardon")  # fmt: skip
DEFAULT_MACROS = [
    {"label": "day", "cmd": "time set day", "icon": "sun"},
    {"label": "night", "cmd": "time set night", "icon": "moon-stars"},
    {"label": "clear", "cmd": "weather clear", "icon": "cloud-sun"},
    {"label": "save", "cmd": "save-all", "icon": "floppy"},
    {"label": "list", "cmd": "list", "icon": "people"},
    {"label": "whitelist_on", "cmd": "whitelist on", "icon": "shield-check"},
]
PROPERTIES = [  # (klucz, typ) — reszta trafia do „Pozostałe”
    ("motd", "text"), ("max-players", "number"), ("difficulty", "peaceful|easy|normal|hard"), ("gamemode", "survival|creative|adventure|spectator"),
    ("online-mode", "bool"), ("white-list", "bool"), ("enforce-whitelist", "bool"), ("pvp", "bool"), ("allow-flight", "bool"),
    ("allow-nether", "bool"), ("hardcore", "bool"), ("enable-command-block", "bool"), ("spawn-protection", "number"),
    ("view-distance", "number"), ("simulation-distance", "number"), ("level-seed", "text"),
]  # fmt: skip
NONCES = hmac_auth.NonceCache(window=WINDOW)


class Offline(Exception):
    """Plugin nie jest połączony (serwer wyłączony albo bez pluginu)."""


class BridgeError(Exception):
    """Plugin odpowiedział błędem."""


@dataclass
class _Link:
    queue: list[dict] = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    waiting: dict[str, asyncio.Future] = field(default_factory=dict)
    seen: float = 0.0
    written: float = 0.0


_links: dict[int, _Link] = {}
_pair_attempts: dict[str, list[float]] = {}


def link(server_id: int) -> _Link:
    return _links.setdefault(server_id, _Link())


# --- dane ---------------------------------------------------------------------------------------------


def servers(panel: Any) -> list[dict]:
    return panel.db.fetchall("SELECT * FROM minecraft_servers ORDER BY name")


def server(panel: Any, server_id: int) -> dict | None:
    return panel.db.get("minecraft_servers", server_id)


def online(row: dict) -> bool:
    seen = max(link(row["id"]).seen, row.get("last_seen") or 0)
    return time.time() - seen < ONLINE_SECONDS


def info(row: dict) -> dict:
    return row.get("info") if isinstance(row.get("info"), dict) else {}


def _info(data: Any) -> dict:
    """Informacje od pluginu (wersja, gracze, TPS) — tylko słownik i w rozsądnym rozmiarze."""
    return data if isinstance(data, dict) and len(json.dumps(data)) < 20000 else {}


def macros(row: dict) -> list[dict]:
    try:
        return json.loads(row.get("macros") or "null") or DEFAULT_MACROS
    except ValueError:
        return DEFAULT_MACROS


def new_pair_code(panel: Any, server_id: int) -> str:
    """Jednorazowy kod parowania (24 h); stary sekret przestaje działać."""
    code = secrets.token_urlsafe(16)
    panel.db.update("minecraft_servers", server_id, pair_hash=hashlib.sha256(code.encode()).hexdigest(), pair_code=panel.sealer.seal(code),
                    pair_expires=time.time() + PAIR_TTL, secret=None)  # fmt: skip
    return code


def pending_code(panel: Any, row: dict) -> str | None:
    """Kod parowania do pokazania w panelu, póki nie został użyty i nie wygasł."""
    if not row.get("pair_code") or (row.get("pair_expires") or 0) < time.time():
        return None
    try:
        return panel.sealer.unseal(row["pair_code"])
    except Exception:  # noqa: BLE001
        return None


def create(panel: Any, name: str, app_id: int | None = None) -> tuple[int, str]:
    server_id = panel.db.insert("minecraft_servers", name=name[:60], app_id=app_id, macros=json.dumps(DEFAULT_MACROS),
                                created_at=time.strftime("%Y-%m-%d %H:%M:%S"))  # fmt: skip
    return server_id, new_pair_code(panel, server_id)


def import_legacy(panel: Any) -> int:
    """Serwery z 1.2.x (JSON w ustawieniach, token jawnym tekstem) → tabela; wymagają ponownego sparowania."""
    raw = panel.db.setting("minecraft_servers")
    if not raw:
        return 0
    items = raw if isinstance(raw, list) else json.loads(raw)
    for item in items:
        server_id, _ = create(panel, str(item.get("name") or item.get("host") or "Minecraft"))
        if item.get("ftp_host"):
            set_ftp(panel, server_id, {"host": item.get("ftp_host"), "port": item.get("ftp_port") or 21, "user": item.get("ftp_user") or "",
                                       "password": item.get("ftp_pass") or "", "tls": bool(item.get("ftp_tls"))})  # fmt: skip
    panel.db.execute("DELETE FROM settings WHERE skey = ?", ["minecraft_servers"])
    return len(items)


def set_ftp(panel: Any, server_id: int, ftp: dict | None) -> None:
    panel.db.update("minecraft_servers", server_id, ftp=panel.sealer.seal(json.dumps(ftp)) if ftp else None)


def get_ftp(panel: Any, row: dict) -> dict | None:
    if not row.get("ftp"):
        return None
    try:
        return json.loads(panel.sealer.unseal(row["ftp"]))
    except Exception:  # noqa: BLE001 — zmieniony klucz panelu
        return None


# --- kanał pluginu ------------------------------------------------------------------------------------


def pair(panel: Any, code: str, data: dict, client_ip: str) -> dict | None:
    """Wymiana kodu na sekret. Limit prób na IP chroni przed zgadywaniem."""
    now = time.time()
    attempts = [t for t in _pair_attempts.get(client_ip, []) if now - t < 600]
    if len(attempts) >= 10:
        return None
    _pair_attempts[client_ip] = [*attempts, now]
    digest = hashlib.sha256(code.encode()).hexdigest()
    row = panel.db.fetchone("SELECT * FROM minecraft_servers WHERE pair_hash = ?", [digest])
    if not row or (row.get("pair_expires") or 0) < now:
        return None
    secret = secrets.token_hex(32)
    panel.db.update("minecraft_servers", row["id"], secret=panel.sealer.seal(secret), pair_hash=None, pair_code=None, pair_expires=None,
                    info=_info(data.get("info")), last_seen=now)  # fmt: skip
    _pair_attempts.pop(client_ip, None)
    return {"server_id": row["id"], "secret": secret}


def secret_of(panel: Any, row: dict) -> bytes | None:
    if not row or not row.get("secret"):
        return None
    try:
        return bytes.fromhex(panel.sealer.unseal(row["secret"]))
    except Exception:  # noqa: BLE001
        return None


def verify(panel: Any, path: str, body: bytes, headers: Any) -> dict:
    """Wiersz serwera dla poprawnie podpisanego żądania pluginu; inaczej `SignatureError`."""
    key = headers.get(hmac_auth.H_KEY) or ""
    row = server(panel, int(key)) if key.isdigit() else None
    secret = secret_of(panel, row)
    if not secret:
        raise hmac_auth.SignatureError("unknown server")
    hmac_auth.verify(secret, "POST", path, body, headers, nonces=NONCES, window=WINDOW)
    return row


def sign_response(secret: bytes, path: str, body: bytes) -> dict[str, str]:
    """Odpowiedź panelu też jest podpisana (metoda „RESPONSE”), więc plugin nie wykona podrzuconego polecenia.

    `path` zawiera nonce żądania pluginu („/minecraft/bridge/poll#<nonce>”) — odpowiedzi nie da się podsunąć
    pod inne zapytanie, nawet po restarcie pluginu (gdy jego pamięć nonce jest pusta)."""
    return hmac_auth.sign(secret, "RESPONSE", path, body, key_id="panel")


async def poll(panel: Any, row: dict, payload: dict) -> list[dict]:
    """Przyjmuje wyniki i informacje o serwerze, potem czeka (do 25 s) na polecenia."""
    state = link(row["id"])
    now = time.time()
    state.seen = now
    for result in payload.get("results") or []:
        future = state.waiting.pop(str(result.get("id")), None)
        if future and not future.done():
            future.set_result(result)
    if now - state.written > 20 or payload.get("info"):
        fields: dict[str, Any] = {"last_seen": now}
        if payload.get("info"):
            fields["info"] = _info(payload["info"])
        panel.db.update("minecraft_servers", row["id"], **fields)
        state.written = now
    if not state.queue:
        state.wake.clear()
        try:
            await asyncio.wait_for(state.wake.wait(), POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    commands, state.queue = state.queue[:20], state.queue[20:]
    return commands


async def call(panel: Any, row: dict, op: str, args: dict | None = None, timeout: float = 20) -> dict:
    """Polecenie dla pluginu; wynik (`data`) albo `Offline` / `BridgeError`."""
    if not row.get("secret") or not online(row):
        raise Offline
    state = link(row["id"])
    command = {"id": secrets.token_hex(8), "op": op, "args": args or {}}
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    state.waiting[command["id"]] = future
    state.queue.append(command)
    state.wake.set()
    try:
        result = await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError as exc:
        state.waiting.pop(command["id"], None)
        state.queue = [c for c in state.queue if c["id"] != command["id"]]
        raise Offline from exc
    if not result.get("ok"):
        raise BridgeError(str(result.get("error") or "error")[:300])
    return result.get("data") or {}


def player_command(action: str, name: str, reason: str = "") -> tuple[str, dict]:
    """Akcja na graczu po walidacji nazwy — (op, args) dla pluginu."""
    if not PLAYER.match(name or "") or action not in PLAYER_ACTIONS:
        raise ValueError("bad player action")
    return "player", {"name": name, "action": action, "reason": re.sub(r"[\r\n]", " ", reason)[:120]}


def clean_command(text: str) -> str:
    """Jedna linia, bez wiodącego „/”."""
    line = (text or "").replace("\r", " ").replace("\n", " ").strip().lstrip("/")
    if not line or len(line) > 1000:
        raise ValueError("bad command")
    return line


def clean_path(path: str) -> str:
    return posixpath.normpath("/" + (path or "/").strip("/")).replace("//", "/")


# --- FTP: pliki, gdy serwer (a z nim plugin) jest wyłączony ----------------------------------------------


class Ftp:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    def _connect(self) -> ftplib.FTP:
        cls = ftplib.FTP_TLS if self.cfg.get("tls") else ftplib.FTP
        ftp = cls(timeout=10)
        ftp.connect(self.cfg["host"], int(self.cfg.get("port") or 21))
        ftp.login(self.cfg.get("user") or "anonymous", self.cfg.get("password") or "")
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        ftp.encoding = "utf-8"
        return ftp

    def test(self) -> str:
        with self._connect() as ftp:
            return ftp.getwelcome() or "OK"

    def list(self, path: str) -> list[dict]:
        items = []
        with self._connect() as ftp:
            try:
                for name, facts in ftp.mlsd(path, facts=["type", "size"]):
                    if name in (".", "..") or facts.get("type") in ("cdir", "pdir"):
                        continue
                    is_dir = facts.get("type") == "dir"
                    items.append({"name": name, "path": posixpath.join(path, name), "is_dir": is_dir, "size": int(facts.get("size") or 0)})
            except ftplib.error_perm:  # serwer bez MLSD
                lines: list[str] = []
                ftp.retrlines(f"LIST {path}", lines.append)
                for line in lines:
                    parts = line.split(None, 8)
                    if len(parts) < 9 or parts[8] in (".", ".."):
                        continue
                    name = parts[8].split(" -> ")[0]
                    items.append({"name": name, "path": posixpath.join(path, name), "is_dir": line.startswith("d"),
                                  "size": int(parts[4]) if parts[4].isdigit() else 0})  # fmt: skip
        return sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower()))

    def read(self, path: str, limit: int = FILE_LIMIT) -> bytes:
        buf = io.BytesIO()

        def keep(chunk: bytes) -> None:
            if buf.tell() + len(chunk) > limit:
                raise ValueError("too big")
            buf.write(chunk)

        with self._connect() as ftp:
            ftp.retrbinary(f"RETR {path}", keep)
        return buf.getvalue()

    def write(self, path: str, data: bytes) -> None:
        with self._connect() as ftp:
            ftp.storbinary(f"STOR {path}", io.BytesIO(data))

    def delete(self, path: str, is_dir: bool) -> None:
        with self._connect() as ftp:
            (ftp.rmd if is_dir else ftp.delete)(path)

    def mkdir(self, path: str) -> None:
        with self._connect() as ftp:
            ftp.mkd(path)


async def ftp_call(cfg: dict, method: str, *args: Any) -> Any:
    return await asyncio.to_thread(getattr(Ftp(cfg), method), *args)
