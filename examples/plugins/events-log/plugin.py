"""Przykładowy plugin Deployry: dziennik zdarzeń i webhook.

Pokazuje całe PluginAPI: stronę z pozycją w menu, zdarzenia, ustawienia i własne tłumaczenia
(`locales/` → klucze `plugin.events-log.*`, w kodzie `api.t("title")`).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import httpx
from fastapi import Form, Request

from deployra_panel.web import toast

LIMIT = 200


def summary(data: dict[str, Any]) -> dict[str, Any]:
    """Bez sekretów: wiersz aplikacji ma w sobie env i sekret webhooka Gita."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = {k: value[k] for k in ("id", "name", "slug", "status", "domains", "target", "kind") if k in value}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def setup(api) -> None:
    events: deque[dict[str, Any]] = deque(maxlen=LIMIT)
    router = api.router()  # /plugins/events-log/…

    async def on_event(event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, **summary(data)}
        events.appendleft({"ts": time.time(), **payload})
        url = api.setting("webhook")
        if url:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json=payload)

    api.on("*", on_event)
    api.add_nav("title", "/plugins/events-log", "journal-text")

    @router.get("")
    def page(request: Request):
        return api.render(request, "index.html", title=api.t("title"), events=list(events), webhook=api.setting("webhook", ""))

    @router.post("/webhook")
    def save_webhook(url: str = Form("")):
        url = url.strip()
        if url and not url.startswith(("http://", "https://")):
            return toast(api.t("bad_url"), "err", status=400)
        api.set_setting("webhook", url)
        return toast(api.t("saved"))

    @router.post("/test")
    def test_event():
        api.notify(api.t("test_message"))
        return toast(api.t("test_sent"))
