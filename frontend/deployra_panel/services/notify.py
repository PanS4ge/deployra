"""Kanały powiadomień: panel, webhook (JSON), Discord, Slack, Telegram, ntfy, Gotify, Pushover i e-mail (SMTP).

Kanały definiuje się w Ustawienia → Alerty; każda reguła alertu i każde zdarzenie (monitor padł,
wdrożenie nie wyszło, nowa wersja…) ma własną listę kanałów. "panel" to wbudowany kanał (dzwonek).
Konfiguracja kanału (tokeny, hasła) leży w bazie zaszyfrowana.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from ..db import now
from ..i18n import t

PANEL = "panel"
LEVEL_COLORS = {"ok": 0x22C55E, "info": 0x22D3EE, "warn": 0xF59E0B, "err": 0xF43F5E}
LEVEL_ICONS = {"ok": "🟢", "info": "🔵", "warn": "🟠", "err": "🔴"}


@dataclass(frozen=True)
class Field:
    name: str
    kind: str = "text"  # text / url / secret / number / select
    required: bool = True
    placeholder: str = ""
    options: tuple[str, ...] = ()


KINDS: dict[str, dict[str, Any]] = {
    "webhook": {"icon": "send", "fields": (Field("url", "url", placeholder="https://example.com/hook"), Field("secret", "secret", False))},
    "discord": {"icon": "discord", "fields": (Field("url", "url", placeholder="https://discord.com/api/webhooks/…"),)},
    "slack": {"icon": "slack", "fields": (Field("url", "url", placeholder="https://hooks.slack.com/services/…"),)},
    "telegram": {"icon": "telegram", "fields": (Field("token", "secret", placeholder="123456:ABC…"), Field("chat_id", placeholder="-1001234567890"))},
    "ntfy": {"icon": "bell", "fields": (Field("url", "url", placeholder="https://ntfy.sh/deployra-alerts"), Field("token", "secret", False))},
    "gotify": {"icon": "broadcast", "fields": (Field("url", "url", placeholder="https://gotify.example.com"), Field("token", "secret"))},
    "pushover": {"icon": "phone-vibrate", "fields": (Field("user"), Field("token", "secret"))},
    "email": {"icon": "envelope", "fields": (
        Field("host", placeholder="smtp.example.com"), Field("port", "number", placeholder="587"),
        Field("security", "select", options=("starttls", "ssl", "none")), Field("username", required=False),
        Field("password", "secret", False), Field("sender", placeholder="deployra@example.com"), Field("to", placeholder="admin@example.com"),
    )},
}  # fmt: skip

STATUS: dict[int, tuple[float, str | None]] = {}  # kanał → (kiedy, błąd ostatniej wysyłki)


@dataclass
class Message:
    title: str
    text: str
    level: str = "info"
    event: str = "test"
    data: dict[str, Any] = field(default_factory=dict)

    def plain(self) -> str:
        return f"{LEVEL_ICONS.get(self.level, '')} {self.title}\n{self.text}".strip()

    def payload(self) -> dict[str, Any]:
        return {"event": self.event, "title": self.title, "text": self.text, "level": self.level, "data": self.data, "ts": time.time()}


# --- kanały w bazie ----------------------------------------------------------------------------


def channels(panel: Any) -> list[dict[str, Any]]:
    rows = panel.db.fetchall("SELECT * FROM notify_channels ORDER BY name")
    for row in rows:
        row["status"] = STATUS.get(row["id"])
    return rows


def channel_config(panel: Any, row: dict[str, Any]) -> dict[str, str]:
    return json.loads(panel.sealer.unseal(row["config"]) or "{}") if row.get("config") else {}


def validate(kind: str, values: dict[str, str], previous: dict[str, str] | None = None) -> dict[str, str]:
    if kind not in KINDS:
        raise ValueError(t("notify.bad_kind"))
    config: dict[str, str] = {}
    for spec in KINDS[kind]["fields"]:
        value = str(values.get(spec.name) or "").strip()
        if not value and spec.kind == "secret" and previous:  # puste pole hasła = bez zmian
            value = previous.get(spec.name, "")
        if spec.required and not value:
            raise ValueError(t("notify.field_required", field=t(f"notify.field.{spec.name}")))
        if value and spec.kind == "url" and not value.startswith(("http://", "https://")):
            raise ValueError(t("notify.bad_url"))
        if value and spec.kind == "number" and not value.isdigit():
            raise ValueError(t("notify.bad_number", field=t(f"notify.field.{spec.name}")))
        if spec.kind == "select" and value and value not in spec.options:
            raise ValueError(t("notify.bad_choice"))
        config[spec.name] = value
    return config


def save_channel(panel: Any, name: str, kind: str, values: dict[str, str], channel_id: int | None = None) -> int:
    previous = None
    if channel_id:
        row = panel.db.get("notify_channels", channel_id)
        if not row:
            raise LookupError(t("notify.not_found"))
        previous = channel_config(panel, row)
    config = validate(kind, values, previous)
    sealed = panel.sealer.seal(json.dumps(config))
    name = (name or "").strip()[:60] or t(f"notify.kind.{kind}")
    if channel_id:
        panel.db.update("notify_channels", channel_id, name=name, kind=kind, config=sealed)
        return channel_id
    return panel.db.insert("notify_channels", name=name, kind=kind, config=sealed, enabled=1, created_at=now())


def migrate_legacy_webhook(panel: Any) -> None:
    """Dawne ustawienie `notify_webhook` → kanał, który dostaje wszystkie zdarzenia (jak wcześniej)."""
    url = (panel.db.setting("notify_webhook") or "").strip()
    if not url:
        return
    from . import alerts

    kind = guess_kind(url)
    channel_id = panel.db.insert("notify_channels", name="Webhook", kind=kind, config=panel.sealer.seal(json.dumps({"url": url})),
                                 enabled=1, created_at=now())  # fmt: skip
    alerts.add_channel_everywhere(panel, channel_id)
    panel.db.set_setting("notify_webhook", "")
    logger.info(f"Webhook powiadomień przeniesiony do kanału #{channel_id} ({kind})")


def guess_kind(url: str) -> str:
    """Dawne pojedyncze pole „Webhook powiadomień” → rodzaj kanału."""
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        return "discord"
    if "hooks.slack.com" in url:
        return "slack"
    if "ntfy" in url:
        return "ntfy"
    return "webhook"


# --- wysyłka ------------------------------------------------------------------------------------


async def send(kind: str, config: dict[str, str], message: Message, client: httpx.AsyncClient) -> None:
    if kind == "webhook":
        body = json.dumps(message.payload(), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "Deployra"}
        if config.get("secret"):
            headers["X-Deployra-Signature"] = "sha256=" + hmac.new(config["secret"].encode(), body, hashlib.sha256).hexdigest()
        response = await client.post(config["url"], content=body, headers=headers)
    elif kind == "discord":
        embed = {"title": message.title[:256], "description": message.text[:4000], "color": LEVEL_COLORS.get(message.level, 0x8B5CF6)}
        response = await client.post(config["url"], json={"username": "Deployra", "embeds": [embed]})
    elif kind == "slack":
        response = await client.post(config["url"], json={"text": f"*{message.title}*\n{message.text}"})
    elif kind == "telegram":
        text = f"<b>{html.escape(message.title)}</b>\n{html.escape(message.text)}"
        response = await client.post(f"https://api.telegram.org/bot{config['token']}/sendMessage",
                                     json={"chat_id": config["chat_id"], "text": text, "parse_mode": "HTML"})  # fmt: skip
    elif kind == "ntfy":
        # publikacja JSON-em na adres serwera — nagłówki HTTP nie zniosą polskich znaków w tytule
        parts = urlsplit(config["url"])
        base, _, topic = parts.path.rstrip("/").rpartition("/")
        headers = {"Authorization": f"Bearer {config['token']}"} if config.get("token") else {}
        tags = {"ok": ["white_check_mark"], "warn": ["warning"], "err": ["rotating_light"]}.get(message.level, ["information_source"])
        body = {"topic": topic, "title": message.title, "message": message.text, "priority": 4 if message.level == "err" else 3, "tags": tags}
        response = await client.post(f"{parts.scheme}://{parts.netloc}{base}/", json=body, headers=headers)
    elif kind == "gotify":
        response = await client.post(config["url"].rstrip("/") + "/message", headers={"X-Gotify-Key": config["token"]},
                                     json={"title": message.title, "message": message.text, "priority": 8 if message.level == "err" else 5})  # fmt: skip
    elif kind == "pushover":
        response = await client.post("https://api.pushover.net/1/messages.json", data={
            "token": config["token"], "user": config["user"], "title": message.title[:250], "message": message.text[:1000],
            "priority": 1 if message.level == "err" else 0,
        })  # fmt: skip
    elif kind == "email":
        await asyncio.to_thread(_send_mail, config, message)
        return
    else:
        raise ValueError(t("notify.bad_kind"))
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")


def _send_mail(config: dict[str, str], message: Message) -> None:
    mail = EmailMessage()
    mail["Subject"] = f"[Deployra] {message.title}"
    mail["From"] = config["sender"]
    mail["To"] = config["to"]
    mail.set_content(message.text)
    security = config.get("security") or "starttls"
    port = int(config.get("port") or (465 if security == "ssl" else 587 if security == "starttls" else 25))
    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(config["host"], port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(config["host"], port, timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if config.get("username"):
            server.login(config["username"], config.get("password", ""))
        server.send_message(mail)


async def send_to_channel(panel: Any, channel_id: int, message: Message) -> str | None:
    """Wysyła przez jeden kanał; zwraca błąd (albo None) i zapamiętuje go do podglądu w ustawieniach."""
    row = panel.db.get("notify_channels", channel_id)
    if not row:
        return t("notify.not_found")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await send(row["kind"], channel_config(panel, row), message, client)
        error = None
    except Exception as exc:  # noqa: BLE001 — każdy błąd kanału trafia tylko do podglądu i logu
        error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning(f"Powiadomienie ({row['name']}): {error}")
    STATUS[channel_id] = (time.time(), error)
    return error


async def dispatch(panel: Any, targets: list[Any], message: Message) -> None:
    """Rozsyła wiadomość do wybranych kanałów ("panel" + id kanałów); wyłączone kanały pomija."""
    if PANEL in targets:
        panel.notify(f"{LEVEL_ICONS.get(message.level, '')} {message.title} — {message.text}".strip(), message.level)
    ids = [int(x) for x in targets if str(x).isdigit()]
    if not ids:
        return
    enabled = {row["id"] for row in panel.db.fetchall("SELECT id FROM notify_channels WHERE enabled = 1")}
    await asyncio.gather(*(send_to_channel(panel, cid, message) for cid in ids if cid in enabled))
