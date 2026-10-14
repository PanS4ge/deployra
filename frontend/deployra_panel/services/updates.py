"""Wbudowany aktualizator: sprawdzanie wydań, wersje komponentów i aktualizacja w tle.

Panel co kilka godzin pyta źródło wydań (GitHub Releases `owner/repo` albo własny
JSON). Gdy jest nowsza wersja, pokazuje baner „Dostępna aktualizacja” z wyborem:
aktualizuj / później / pomiń tę wersję. „Aktualizuj” uruchamia zadanie, które
po kolei zleca aktualizację węzłom, bramce i na końcu samemu panelowi (węzeł
podmienia się sam, panel i bramka mają obok siebie kontener `deployra-updater`),
i czeka, aż każdy wstanie w nowej wersji. Panel podmienia się ostatni — po restarcie kończy zadanie
powiadomieniem.
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse
from typing import Any

import httpx
from loguru import logger
from markupsafe import Markup, escape

from deployra_common import i18n as i18n_core
from deployra_common.client import RemoteError
from deployra_common.jobs import Job, JobFailed
from deployra_common.updates import is_newer, normalize, parse_version, same_version

from .. import __version__
from ..i18n import t

CHECK_INTERVAL = 6 * 3600
TICK = 600
FIRST_CHECK = 90
POLL = 4.0
REMOTE_TIMEOUT = 25 * 60  # budowanie ze źródeł na słabym serwerze potrafi trwać
DONE_GRACE = 120  # tyle czekamy po „zaktualizowano”, aż komponent zgłosi nową wersję
SNOOZE = 24 * 3600
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GITHUB = "https://api.github.com/repos/"


# --- źródło wydań ------------------------------------------------------------------------------


def source(panel: Any) -> str:
    return str(panel.db.setting("update_repo") or panel.config.update_repo or "").strip()


def validate_source(value: str) -> str:
    value = value.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if value and not (REPO.match(value) or value.startswith(("https://", "http://"))):
        raise ValueError(t("upd.bad_source"))
    return value


async def fetch_latest(src: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Najnowsze wydanie: `{version, name, notes, url, published_at}`."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"Deployra/{__version__}"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers, transport=transport) as client:
        if not REPO.match(src):
            response = await client.get(src)
            response.raise_for_status()
            data = response.json()
            raw = data.get("version") or data.get("tag_name")
            return _release(raw, data.get("name"), data.get("notes") or data.get("body"), data.get("url") or data.get("html_url"),
                            data.get("published_at") or data.get("date"))  # fmt: skip
        response = await client.get(f"{GITHUB}{src}/releases/latest")
        if response.status_code == 404:
            # repozytorium bez wydań → najwyższy tag vX.Y.Z; prywatne repozytorium → tagi publicznego obrazu w GHCR
            tags = await client.get(f"{GITHUB}{src}/tags", params={"per_page": 100})
            names = [tag.get("name") for tag in tags.json() if parse_version(tag.get("name"))] if tags.status_code == 200 else []
            if names:
                best = max(names, key=parse_version)
                return _release(best, None, "", f"https://github.com/{src}/releases/tag/{best}", None)
            best = await _registry_latest(client, f"ghcr.io/{src.split('/')[0].lower()}/deployra-panel")
            if not best:
                raise ValueError(t("upd.no_releases_private", source=src))
            return _release(best, None, "", "", None)
        response.raise_for_status()
        data = response.json()
        return _release(data.get("tag_name"), data.get("name"), data.get("body"), data.get("html_url"), data.get("published_at"))


async def _registry_latest(client: httpx.AsyncClient, image: str) -> str | None:
    """Najwyższy tag X.Y.Z publicznego obrazu (anonimowy token rejestru, jak przy `docker pull`)."""
    host, _, path = image.partition("/")
    token = await client.get(f"https://{host}/token", params={"scope": f"repository:{path}:pull"})
    if token.status_code != 200:
        return None
    listing = await client.get(f"https://{host}/v2/{path}/tags/list", params={"n": 1000},
                               headers={"Authorization": f"Bearer {token.json().get('token', '')}"})  # fmt: skip
    if listing.status_code != 200:
        return None
    names = [tag for tag in listing.json().get("tags") or [] if parse_version(tag)]
    return max(names, key=parse_version) if names else None


def _release(raw: Any, name: Any, notes: Any, url: Any, published: Any) -> dict[str, Any]:
    if parse_version(raw) is None:
        raise ValueError(t("upd.bad_release_version", version=repr(str(raw)[:40])))
    version = normalize(raw)
    link = str(url or "")
    return {
        "version": version,
        "name": str(name or f"Deployra {version}")[:120],
        "notes": str(notes or "")[:20000],
        "url": link if link.startswith(("https://", "http://")) else "",
        "published_at": published,
    }


def _error_text(exc: Exception, src: str) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (403, 429):
            return t("upd.rate_limited")
        if code == 404:
            return t("upd.not_found", source=src)
        return t("upd.http_status", source=src, code=code)
    if isinstance(exc, httpx.TimeoutException):
        return t("upd.timeout")
    if isinstance(exc, httpx.HTTPError):
        return t("upd.offline")
    return str(exc)


async def check(panel: Any, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    src = source(panel)
    previous = panel.db.setting("update_latest") or {}
    result: dict[str, Any] = {"checked_at": time.time(), "source": src, "error": None}
    if not src:
        result["error"] = t("upd.no_source")
    else:
        try:
            result.update(await fetch_latest(src, transport))
        except (httpx.HTTPError, ValueError) as exc:
            result["error"] = _error_text(exc, src)
            if previous.get("source") == src:  # zostawiamy ostatnią znaną wersję — baner nie znika przez chwilowy błąd
                result.update({k: previous[k] for k in ("version", "name", "notes", "url", "published_at") if k in previous})
    panel.db.set_setting("update_latest", result)
    version = result.get("version")
    if version and is_newer(version, __version__) and panel.db.setting("update_notified") != version:
        from . import alerts
        from .notify import Message

        await alerts.event(panel, "update_available", Message(t("alerts.title.update_available", name=version),
                                                              t("upd.notify_available", version=version, current=__version__), "info"))  # fmt: skip
        panel.db.set_setting("update_notified", version)
        panel.plugins.emit("update.available", version=version, current=__version__, release=result)
    return result


# --- baner i stan --------------------------------------------------------------------------


def latest(panel: Any) -> dict[str, Any]:
    return panel.db.setting("update_latest") or {}


def pending(panel: Any) -> dict[str, Any] | None:
    value = panel.db.setting("update_pending")
    if value and time.time() - float(value.get("started") or 0) < 2 * 3600:
        return value
    return None


def target_version(panel: Any) -> str:
    """Do jakiej wersji podciągamy komponenty: najnowsze wydanie albo (gdy go nie znamy) wersja panelu."""
    version = latest(panel).get("version")
    return version if version and is_newer(version, __version__) else __version__


def offer(panel: Any) -> dict[str, Any] | None:
    """Dane do banera albo None (brak aktualizacji, odłożona, pominięta)."""
    running = pending(panel)
    if running:
        return {"pending": running}
    info = latest(panel)
    version = info.get("version")
    if not version or not is_newer(version, __version__):
        return None
    if panel.db.setting("update_skip") == version or float(panel.db.setting("update_snooze") or 0) > time.time():
        return None
    return {**info, "current": __version__, "pending": None}


def notes_html(text: str) -> Markup:
    """Minimalny, bezpieczny podgląd notatek wydania (nagłówki, listy, reszta jako tekst)."""
    out: list[str] = []
    in_list = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if in_list and not bullet:
            out.append("</ul>")
            in_list = False
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{escape(bullet.group(1))}</li>")
        elif line.startswith("#"):
            out.append(f"<h4>{escape(line.lstrip('#').strip())}</h4>")
        elif line.strip():
            out.append(f"<p>{escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("".join(out))


def describe(info: dict[str, Any]) -> str:
    """Opis stanu aktualizatora w języku użytkownika (krok z paczki + szczegóły ze skryptu)."""
    step = info.get("step")
    if not step:
        return str(info.get("message") or "")
    text = t("updater." + step, version=info.get("target") or "")
    detail = info.get("message") or ""
    return f"{text} — {detail}" if info.get("state") == "error" and detail else text


# --- komponenty ----------------------------------------------------------------------------


async def components(panel: Any, log_lines: int = 0) -> list[dict[str, Any]]:
    """Panel, bramka i sparowane węzły z wersją i stanem aktualizatora."""
    items: list[dict[str, Any]] = [
        {"key": "panel", "name": t("upd.comp_panel"), "icon": "window-stack", "online": True, "version": __version__,
         "update": panel.updates.info(log_lines), "client": None, "address": panel.db.setting("panel_domain") or None},
    ]  # fmt: skip
    remote: list[tuple[str, str, str, Any, str | None]] = []
    hub, hub_client = panel.gateway(), panel.hub_client()
    if hub_client:
        remote.append(("gateway", t("upd.comp_gateway"), "diagram-3", hub_client, (hub or {}).get("public_host")))
    edge = panel.edge_row()
    if edge and edge.get("secret"):
        remote.append(("edge", t("upd.comp_edge"), "signpost-split", panel.gateway_client(), edge.get("public_host")))
    for node in panel.db.all("nodes"):
        if node["status"] == "paired" and node.get("secret"):
            address = node.get("tunnel_ip") if node["transport"] == "tunnel" else urlparse(node.get("api_url") or "").hostname
            remote.append((f"node-{node['id']}", node["name"], "hdd-network", panel.node_client(node), address))

    async def fetch(key: str, name: str, icon: str, client: Any, address: str | None) -> dict[str, Any]:
        item = {"key": key, "name": name, "icon": icon, "online": False, "version": None, "update": None, "client": client, "error": None,
                "address": address}  # fmt: skip
        try:
            data = await asyncio.wait_for(client.get("/api/update", params={"log": log_lines} if log_lines else None), 8)
            item.update(online=True, version=data.get("version"), update={k: v for k, v in data.items() if k not in ("service", "version")})
        except RemoteError as exc:
            item["error"] = exc.detail
        except (httpx.HTTPError, asyncio.TimeoutError):
            item["error"] = t("upd.not_responding")
        return item

    items += await asyncio.gather(*(fetch(*args) for args in remote))
    target = target_version(panel)
    for item in items:
        item["behind"] = bool(item["version"] and is_newer(target, item["version"]))
    return items


# --- aktualizacja ------------------------------------------------------------------------------


def start(panel: Any, target: str | None = None, only: set[str] | None = None, auto: bool = False) -> Job:
    if any(job.kind == "update" and not job.done for job in panel.jobs.list()):
        raise LookupError(t("upd.already_running"))
    version = normalize(target or target_version(panel))
    title = t("upd.job_title_auto" if auto else "upd.job_title", version=version)
    return panel.jobs.start("update", title, lambda job: run(panel, job, version, only))


async def run(panel: Any, job: Job, target: str, only: set[str] | None = None) -> dict[str, Any]:
    items = [c for c in await components(panel) if only is None or c["key"] in only]
    todo = []
    for item in items:
        if not item["online"]:
            job.log(t("upd.skip_offline", name=item["name"], error=item.get("error") or "offline"))
        elif not is_newer(target, item["version"]):
            job.log(t("upd.already_at", name=item["name"], version=item["version"]))
        elif not (item["update"] or {}).get("available"):
            job.log(t("upd.no_updater", name=item["name"]))
        else:
            todo.append(item)
    if not todo:
        job.log(t("upd.nothing"))
        return {"updated": []}

    # najpierw węzły (równolegle), potem bramka, na końcu panel — on zrestartuje się ostatni
    nodes = [c for c in todo if c["key"].startswith("node-")]
    results: dict[str, bool] = {}
    if nodes:
        done = await asyncio.gather(*(_remote(job, c, target) for c in nodes))
        results.update({c["name"]: ok for c, ok in zip(nodes, done)})
    for item in (c for c in todo if c["key"] in ("edge", "gateway")):
        results[item["name"]] = await _remote(job, item, target)
    failed = [name for name, ok in results.items() if not ok]
    me = next((c for c in todo if c["key"] == "panel"), None)
    if me and failed:
        job.log(t("upd.panel_stays"))
    elif me:
        results[me["name"]] = await _self(panel, job, target)
        failed = [name for name, ok in results.items() if not ok]
    if failed:
        raise JobFailed(t("upd.failed", names=", ".join(failed)))
    return {"updated": list(results)}


async def _remote(job: Job, item: dict[str, Any], target: str) -> bool:
    name, client = item["name"], item["client"]
    try:
        info = await client.post("/api/update", json={"version": target})
    except RemoteError as exc:
        job.log(f"✖ {name}: {exc.detail}")
        return False
    except httpx.HTTPError:
        job.log(f"✖ {name}: {t('upd.not_responding')}")
        return False
    job.log(f"{name}: {item['version']} → {target} ({t('updates.mode.' + str(info.get('mode')))})")
    deadline = time.monotonic() + REMOTE_TIMEOUT
    last, offline, done_at = None, False, None
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        try:
            data = await asyncio.wait_for(client.get("/api/update"), 10)
        except (RemoteError, httpx.HTTPError, asyncio.TimeoutError):
            if not offline:
                job.log(t("upd.restarting", name=name))
                offline = True
            continue
        offline = False
        if same_version(data.get("version"), target):
            job.log(t("upd.running_version", name=name, version=target))
            return True
        message = describe(data)
        if message and message != last:
            job.log(f"{name}: {message}")
            last = message
        if not same_version(data.get("target"), target):
            continue
        if data.get("state") == "error":
            await _log_tail(job, name, client)
            return False
        if data.get("state") == "done":
            done_at = done_at or time.monotonic()
            if time.monotonic() - done_at > DONE_GRACE:
                job.log(t("upd.still_old", name=name, version=data.get("version"), target=target))
                return False
    job.log(t("upd.wait_timeout", name=name, minutes=REMOTE_TIMEOUT // 60))
    return False


async def _log_tail(job: Job, name: str, client: Any) -> None:
    try:
        data = await client.get("/api/update", params={"log": 15})
    except (RemoteError, httpx.HTTPError):
        data = {}
    job.log(f"✖ {name}: {describe(data) or t('upd.update_failed')}")
    for line in data.get("log") or []:
        job.log(f"    {line}")


async def _self(panel: Any, job: Job, target: str) -> bool:
    agent = panel.updates
    panel.db.set_setting("update_pending", {"target": target, "job_id": job.id, "started": time.time()})
    try:
        agent.request(target)
    except (LookupError, ValueError) as exc:
        panel.db.set_setting("update_pending", None)
        job.log(f"✖ Panel: {exc}")
        return False
    job.log(t("upd.panel_requested", current=__version__, version=target))
    deadline = time.monotonic() + REMOTE_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        info = agent.info()
        message = describe(info)
        if message and message != last:
            job.log(f"Panel: {message}")
            last = message
        if info["state"] == "error":
            for line in agent.info(15).get("log") or []:
                job.log(f"    {line}")
            panel.db.set_setting("update_pending", None)
            return False
        if info["state"] == "done":
            # kontener już podmieniony, a my nadal żyjemy? to znaczy, że obraz się nie zmienił
            await asyncio.sleep(DONE_GRACE)
            job.log(t("upd.panel_not_replaced", version=__version__))
            panel.db.set_setting("update_pending", None)
            return False
    job.log(t("upd.wait_timeout", name="Panel", minutes=REMOTE_TIMEOUT // 60))
    panel.db.set_setting("update_pending", None)
    return False


def finish_pending(panel: Any) -> None:
    """Po starcie panelu: czy aktualizacja, w trakcie której się zrestartował, się udała."""
    value = panel.db.setting("update_pending")
    if not value:
        return
    target = value.get("target")
    job_id = value.get("job_id")
    job = panel.jobs.get(job_id) if job_id else None

    if same_version(__version__, target) or is_newer(__version__, target):
        panel.notify(t("upd.notify_done", version=__version__), "ok")
        if job:
            job.status = "ok"
            job.finished = time.time()
            job.error = None
            job.log(f"✔ Panel: {t('upd.notify_done', version=__version__)}")
            if hasattr(panel.jobs, "save_job"):
                panel.jobs.save_job(job)
        panel.db.set_setting("update_pending", None)
        return
    info = panel.updates.info()
    if info["state"] == "error":
        err_msg = describe(info) or "Błąd aktualizacji"
        panel.notify(t("upd.notify_failed", version=target, error=err_msg), "err")
        if job:
            job.status = "error"
            job.error = err_msg
            job.finished = time.time()
            job.log(f"✖ Panel: {err_msg}")
            if hasattr(panel.jobs, "save_job"):
                panel.jobs.save_job(job)
    elif time.time() - float(value.get("started") or 0) > 3600:
        panel.notify(t("upd.notify_unfinished", version=target), "err")
        if job:
            job.status = "error"
            job.error = "Przekroczono limit czasu aktualizacji"
            job.finished = time.time()
            if hasattr(panel.jobs, "save_job"):
                panel.jobs.save_job(job)
    else:
        return  # aktualizacja jeszcze trwa (np. buduje się obraz)
    panel.db.set_setting("update_pending", None)


async def loop(panel: Any) -> None:
    """Co TICK sprawdza, czy minął interwał; przy nowej wersji i włączonym auto-update — aktualizuje."""
    await asyncio.sleep(FIRST_CHECK)
    while True:
        i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)
        try:
            if panel.db.setting("update_check", True) and source(panel):
                last = float(latest(panel).get("checked_at") or 0)
                if time.time() - last > CHECK_INTERVAL:
                    info = await check(panel)
                    version = info.get("version")
                    if (version and not info.get("error") and is_newer(version, __version__) and panel.db.setting("update_auto", False)
                            and panel.db.setting("update_skip") != version and not pending(panel)):  # fmt: skip
                        logger.info(f"Automatyczna aktualizacja do {version}")
                        start(panel, version, auto=True)
        except Exception as exc:  # noqa: BLE001 — pętla nie może paść
            logger.error(f"Sprawdzanie aktualizacji: {exc}")
        await asyncio.sleep(TICK)
