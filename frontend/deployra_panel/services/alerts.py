"""Alerty: reguły z progami (CPU, RAM, dysk, węzeł offline, pamięć aplikacji, OOM, pętla restartów) i zdarzenia.

Ustawienia (`alerts` w bazie): {reguła: {enabled, threshold, minutes, channels, auto_move}} — każda reguła
ma własną listę kanałów ("panel" i/lub id kanałów z `notify_channels`). Próg musi trwać `minutes`
minut, zanim alert wystrzeli; wraca do normy dopiero poniżej progu minus histereza (bez trzepotania).
Aktywne alerty leżą w tabeli `alerts` (status firing/resolved), czasy przekroczeń — w pamięci.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..i18n import t
from .notify import PANEL, Message, dispatch

HYSTERESIS = 5.0  # punkty procentowe poniżej progu, żeby alert wrócił do normy
KEEP = 30 * 86400


@dataclass(frozen=True)
class Rule:
    target: str  # node / app / event
    threshold: float | None = None
    minutes: int | None = None
    unit: str = "%"
    level: str = "warn"
    can_auto_move: bool = False


RULES: dict[str, Rule] = {
    "node_cpu": Rule("node", 90, 10),
    "node_ram": Rule("node", 90, 5, can_auto_move=True),
    "node_disk": Rule("node", 90, 0, level="err"),
    "node_offline": Rule("node", None, 3, level="err"),
    "app_memory": Rule("app", 90, 5),
    "app_restarts": Rule("app", 3, 10, unit="×", level="err"),
    "app_oom": Rule("app", level="err"),
    "monitor_down": Rule("event", level="err"),
    "deploy_failed": Rule("event", level="err"),
    "app_moved": Rule("event", level="info"),
    "update_available": Rule("event", level="info"),
}


def settings(panel: Any) -> dict[str, dict[str, Any]]:
    stored = panel.db.setting("alerts") or {}
    out = {}
    for rule_id, rule in RULES.items():
        current = stored.get(rule_id) or {}
        out[rule_id] = {
            "enabled": bool(current.get("enabled", True)),
            "threshold": current.get("threshold", rule.threshold),
            "minutes": current.get("minutes", rule.minutes),
            "channels": list(current.get("channels", [PANEL])),
            "auto_move": bool(current.get("auto_move", False)) and rule.can_auto_move,
        }
    return out


def save_settings(panel: Any, values: dict[str, dict[str, Any]]) -> None:
    clean: dict[str, dict[str, Any]] = {}
    valid = {PANEL} | {str(row["id"]) for row in panel.db.fetchall("SELECT id FROM notify_channels")}
    for rule_id, rule in RULES.items():
        value = values.get(rule_id) or {}
        entry: dict[str, Any] = {"enabled": bool(value.get("enabled")), "channels": [c for c in value.get("channels", []) if str(c) in valid]}
        entry["channels"] = [c if c == PANEL else int(c) for c in entry["channels"]]
        if rule.threshold is not None:
            threshold = float(value.get("threshold") or rule.threshold)
            if not (1 <= threshold <= (100 if rule.unit == "%" else 1000)):
                raise ValueError(t("alerts.bad_threshold"))
            entry["threshold"] = threshold
        if rule.minutes is not None:
            minutes = int(value.get("minutes") or 0)
            if not 0 <= minutes <= 1440:
                raise ValueError(t("alerts.bad_minutes"))
            entry["minutes"] = minutes
        if rule.can_auto_move:
            entry["auto_move"] = bool(value.get("auto_move"))
        clean[rule_id] = entry
    panel.db.set_setting("alerts", clean)


def add_channel_everywhere(panel: Any, channel_id: int) -> None:
    """Nowy (albo zmigrowany) kanał dostaje wszystkie zdarzenia — potem można to zawęzić."""
    current = settings(panel)
    for value in current.values():
        if channel_id not in value["channels"]:
            value["channels"].append(channel_id)
    panel.db.set_setting("alerts", current)


def remove_channel(panel: Any, channel_id: int) -> None:
    current = settings(panel)
    for value in current.values():
        value["channels"] = [c for c in value["channels"] if c != channel_id]
    panel.db.set_setting("alerts", current)


# --- zdarzenia jednorazowe ------------------------------------------------------------------------


async def event(panel: Any, rule_id: str, message: Message) -> bool:
    config = settings(panel).get(rule_id)
    if not config or not config["enabled"]:
        return False
    message.event = rule_id
    await dispatch(panel, config["channels"], message)
    return True


def _active(panel: Any) -> dict[tuple[str, str, int], dict[str, Any]]:
    rows = panel.db.fetchall("SELECT * FROM alerts WHERE status = 'firing'")
    return {(r["rule"], r["target_kind"], r["target_id"]): r for r in rows}


async def fire(panel: Any, rule_id: str, kind: str, target_id: int, name: str, value: float | None, text: str, data: dict | None = None) -> int:
    alert_id = panel.db.insert("alerts", rule=rule_id, target_kind=kind, target_id=target_id, target_name=name, status="firing",
                               message=text[:500], value=value, started_at=time.time())  # fmt: skip
    await event(panel, rule_id, Message(t(f"alerts.title.{rule_id}", name=name), text, RULES[rule_id].level, data=data or {}))
    return alert_id


async def resolve(panel: Any, alert: dict[str, Any], text: str) -> None:
    panel.db.update("alerts", alert["id"], status="resolved", resolved_at=time.time())
    await event(panel, alert["rule"], Message(t("alerts.resolved_title", name=alert["target_name"]), text, "ok"))


async def monitor_change(panel: Any, monitor: dict[str, Any], status: str, detail: str) -> None:
    """Monitor uptime padł / wstał — alert z historią zamiast samego powiadomienia."""
    key = ("monitor_down", "monitor", monitor["id"])
    active = _active(panel).get(key)
    kind = t("uptime.kind." + monitor["kind"])
    if status == "down" and not active:
        await fire(panel, "monitor_down", "monitor", monitor["id"], monitor["name"], None,
                   t("alerts.text.monitor_down", kind=kind, detail=detail))  # fmt: skip
    elif status == "up" and active:
        await resolve(panel, active, t("alerts.text.monitor_up", kind=kind, detail=detail))


def cleanup(panel: Any) -> None:
    panel.db.execute("DELETE FROM alerts WHERE status = 'resolved' AND started_at < ?", [time.time() - KEEP])


def recent(panel: Any, limit: int = 30) -> list[dict[str, Any]]:
    return panel.db.fetchall("SELECT * FROM alerts ORDER BY CASE WHEN status = 'firing' THEN 0 ELSE 1 END, started_at DESC LIMIT ?", [limit])


def firing(panel: Any) -> list[dict[str, Any]]:
    return panel.db.fetchall("SELECT * FROM alerts WHERE status = 'firing' ORDER BY started_at DESC")


# --- reguły z progami (wywoływane co minutę z pętli zasobów) --------------------------------------


@dataclass
class Tracker:
    since: dict[tuple[str, str, int], float] = field(default_factory=dict)
    restarts: dict[int, deque[tuple[float, int]]] = field(default_factory=dict)
    oom_seen: dict[int, str] = field(default_factory=dict)
    offline_since: dict[int, float] = field(default_factory=dict)


async def _threshold(panel: Any, tracker: Tracker, active: dict, rule_id: str, kind: str, target_id: int, name: str,
                     value: float | None, now_ts: float, **text: Any) -> str | None:  # fmt: skip
    """Zwraca "fired" / "resolved" / None. `value` None = brak danych (nie zmieniamy stanu)."""
    config = settings_cache(panel)[rule_id]
    key = (rule_id, kind, target_id)
    alert = active.get(key)
    if value is None or not config["enabled"]:
        if alert and not config["enabled"]:
            await resolve(panel, alert, t("alerts.disabled"))
            return "resolved"
        return None
    threshold = float(config["threshold"])
    if value >= threshold:
        since = tracker.since.setdefault(key, now_ts)
        hold = 0 if rule_id == "app_restarts" else (config["minutes"] or 0)  # przy restartach „minuty” to okno liczenia
        if not alert and now_ts - since >= hold * 60:
            message = t(f"alerts.text.{rule_id}", value=_fmt(value), threshold=_fmt(threshold), minutes=config["minutes"], **text)
            active[key] = {"id": await fire(panel, rule_id, kind, target_id, name, value, message), "rule": rule_id, "target_name": name}
            return "fired"
        return None
    tracker.since.pop(key, None)
    hysteresis = HYSTERESIS if RULES[rule_id].unit == "%" else 0.5
    if alert and value < threshold - hysteresis:
        await resolve(panel, alert, t(f"alerts.ok.{rule_id}", value=_fmt(value), **text))
        active.pop(key, None)
        return "resolved"
    return None


def _fmt(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 or value >= 10 else f"{value:.1f}"


_CACHE: dict[int, tuple[float, dict]] = {}


def settings_cache(panel: Any) -> dict[str, dict[str, Any]]:
    """Ustawienia czytane raz na obieg pętli (wiele węzłów i aplikacji)."""
    cached = _CACHE.get(id(panel))
    if cached and time.time() - cached[0] < 5:
        return cached[1]
    value = settings(panel)
    _CACHE[id(panel)] = (time.time(), value)
    return value


def forget_settings_cache() -> None:
    _CACHE.clear()


async def evaluate_node(panel: Any, tracker: Tracker, node: dict[str, Any], usage: dict[str, Any] | None, apps: dict[str, dict],
                        now_ts: float) -> list[str]:  # fmt: skip
    """Sprawdza reguły węzła i jego aplikacji; zwraca listę reguł, które właśnie wystrzeliły."""
    active = _active(panel)
    fired: list[str] = []
    config = settings_cache(panel)
    key = ("node_offline", "node", node["id"])
    if usage is None:
        since = tracker.offline_since.setdefault(node["id"], now_ts)
        offline = config["node_offline"]
        if offline["enabled"] and key not in active and now_ts - since >= (offline["minutes"] or 0) * 60:
            minutes = max(1, round((now_ts - since) / 60))
            await fire(panel, "node_offline", "node", node["id"], node["name"], None, t("alerts.text.node_offline", minutes=minutes))
            fired.append("node_offline")
        return fired
    tracker.offline_since.pop(node["id"], None)
    if key in active:
        await resolve(panel, active[key], t("alerts.ok.node_offline"))

    host = usage.get("host") or {}
    for rule_id, value in (("node_cpu", host.get("cpu")), ("node_ram", host.get("ram")), ("node_disk", host.get("disk"))):
        if await _threshold(panel, tracker, active, rule_id, "node", node["id"], node["name"], value, now_ts) == "fired":
            fired.append(rule_id)

    for slug, stats in (usage.get("apps") or {}).items():
        app = apps.get(slug)
        if not app:
            continue
        name = app["name"]
        if stats.get("limit") and stats.get("mem") is not None:
            percent = stats["mem"] / stats["limit"] * 100
            await _threshold(panel, tracker, active, "app_memory", "app", app["id"], name, percent, now_ts,
                             mem=_mb(stats["mem"]), limit=_mb(stats["limit"]))  # fmt: skip
        # zabity za brak pamięci — każde nowe OOM (inny czas zakończenia) to osobny alert
        oom, seen = stats.get("oom_at"), tracker.oom_seen.get(app["id"])
        if oom and oom != seen and (seen is not None or _fresh(oom, now_ts)):  # po starcie panelu tylko świeże OOM
            if config["app_oom"]["enabled"]:
                alert_id = await fire(panel, "app_oom", "app", app["id"], name, None, t("alerts.text.app_oom", limit=_mb(stats.get("limit"))))
                panel.db.update("alerts", alert_id, status="resolved", resolved_at=now_ts)  # zdarzenie, nie stan
                fired.append("app_oom")
        if oom:
            tracker.oom_seen[app["id"]] = oom
        # pętla restartów: ile restartów w oknie `minutes`
        window = (config["app_restarts"]["minutes"] or 10) * 60
        history = tracker.restarts.setdefault(app["id"], deque())
        history.append((now_ts, int(stats.get("restarts") or 0)))
        while history and now_ts - history[0][0] > window:
            history.popleft()
        count = max(0, history[-1][1] - history[0][1])
        await _threshold(panel, tracker, active, "app_restarts", "app", app["id"], name, float(count), now_ts, count=count)
    return fired


def _fresh(stamp: str, now_ts: float) -> bool:
    """Czas z Dockera (2026-10-11T09:00:00.123456789Z) młodszy niż 10 minut."""
    import calendar
    import re

    match = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", stamp or "")
    if not match:
        return False
    return now_ts - calendar.timegm(tuple(int(x) for x in match.groups()) + (0, 0, 0)) < 600


def _mb(value: Any) -> str:
    return f"{float(value) / 1024 / 1024:.0f} MB" if value else "—"
