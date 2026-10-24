"""Aktualizacje obrazów aplikacji: nowy build tego samego tagu i nowsze wersje przypiętego tagu.

Węzeł podaje digesty obrazów kontenerów (`/api/images/digests`; dla aplikacji „z plików” — obraz języka, z którego
zbudowano obraz bazowy). Panel pyta rejestr zapytaniem HEAD o manifest tagu (jak Watchtower — Docker Hub nie wlicza
tego do limitu pobrań) i porównuje digest. Dla tagu z dokładną wersją (`grafana/grafana:11.2.0`) czyta też listę tagów
i szuka nowszych wersji o tym samym kształcie (`29.0.7-apache` → `29.0.9-apache`, bez wersji testowych).

Tryb z ustawień (aplikacja może mieć własny): off — nic, notify — strzałka na ikonie i powiadomienie, auto — aktualizacja
sama: nowy build tagu, a z `auto_patch` także nowa wersja poprawkowa (X.Y.Z → X.Y.Z+n). Większych skoków wersji
nie robimy bez człowieka.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from deployra_common import i18n as i18n_core
from deployra_common.client import RemoteError

from .. import __version__
from ..i18n import t

MODES = ("off", "notify", "auto")
POLICIES = ("", *MODES)  # "" — jak w ustawieniach
DEFAULTS = {"mode": "notify", "interval_hours": 12, "auto_patch": False}
INTERVALS = (3, 6, 12, 24)
UPDATABLE = ("image", "compose", "runtime")  # aplikacje z Gita budują obraz z Dockerfile'a — nie ma czego porównać
FIRST_CHECK = 240
TICK = 600
RETRY_FAILED = 1800
PARALLEL = 6
MAX_TAG_PAGES = 20
RETRIES = 3
RETRY_DELAY = 2.0
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json",
))  # fmt: skip
RUNNING: set[int] = set()
_badges: dict[int, tuple[float, dict[int, dict]]] = {}  # id(panel) → (czas, app_id → skrót) — ikony w wielu miejscach strony


# --- ustawienia ----------------------------------------------------------------------------------


def settings(panel: Any) -> dict[str, Any]:
    return {**DEFAULTS, **(panel.db.setting("app_updates") or {})}


def save_settings(panel: Any, mode: str, interval_hours: int, auto_patch: bool) -> None:
    if mode not in MODES or interval_hours not in INTERVALS:
        raise ValueError(t("appupd.bad_settings"))
    panel.db.set_setting("app_updates", {"mode": mode, "interval_hours": interval_hours, "auto_patch": auto_patch})
    _badges.pop(id(panel), None)


def policy(panel: Any, app: dict, config: dict | None = None) -> str:
    """Tryb aktualizacji aplikacji: własny albo z ustawień."""
    own = app.get("update_policy") or ""
    return own if own in MODES else (config or settings(panel))["mode"]


def set_policy(panel: Any, app_id: int, value: str) -> None:
    if value not in POLICIES:
        raise ValueError(t("appupd.bad_settings"))
    panel.db.update("apps", app_id, update_policy=value or None)
    _badges.pop(id(panel), None)


def status(panel: Any) -> dict[str, Any]:
    return panel.db.setting("app_updates_status") or {}


def running(panel: Any) -> bool:
    return id(panel) in RUNNING


# --- obrazy i wersje -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    image: str  # jak w aplikacji / kontenerze
    registry: str  # host API (registry-1.docker.io dla Docker Hub)
    repo: str  # library/nginx
    tag: str


def parse_ref(image: str) -> Ref | None:
    """`nginx:1.27` → registry-1.docker.io, library/nginx, 1.27; obraz po digeście (`@sha256:…`) — None."""
    image = (image or "").strip()
    if not image or "@" in image:
        return None
    first, _, rest = image.partition("/")
    host, path = (first, rest) if rest and ("." in first or ":" in first or first == "localhost") else ("docker.io", image)
    name, tag = (path.rsplit(":", 1) if ":" in path.rsplit("/", 1)[-1] else (path, "latest"))
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
        name = name if "/" in name else f"library/{name}"
    return Ref(image, host, name.lower(), tag)


def with_tag(image: str, tag: str) -> str:
    """Ten sam obraz z innym tagiem (port rejestru w nazwie zostaje)."""
    head, _, last = image.rpartition("/")
    name = last.split(":", 1)[0]
    return f"{head}/{name}:{tag}" if head else f"{name}:{tag}"


_VERSION = re.compile(r"^(v?)(\d+(?:\.\d+){2,3})((?:[-_][A-Za-z0-9.]+)*)$")


def _version(tag: str) -> tuple[str, tuple[int, ...], str] | None:
    match = _VERSION.match(tag)
    if not match:
        return None
    return match.group(1), tuple(int(p) for p in match.group(2).split(".")), match.group(3)


def pinned(tag: str) -> bool:
    """Tag z dokładną wersją (X.Y.Z) — nowsze wydania mają inne tagi; `8.3-apache`, `5-alpine`, `latest` to tagi pływające."""
    return _version(tag) is not None


def newer(tag: str, tags: list[str]) -> dict[str, str | None] | None:
    """Nowsze wersje o tym samym kształcie: `patch` (X.Y.*), `minor` (X.*), `latest` (dowolna)."""
    current = _version(tag)
    if not current:
        return None
    prefix, nums, suffix = current
    found = []
    for candidate in tags:
        parsed = _version(candidate)
        if parsed and parsed[0] == prefix and parsed[2] == suffix and len(parsed[1]) == len(nums) and parsed[1] > nums:
            found.append((parsed[1], candidate))
    if not found:
        return None
    pick = lambda keep: max((f for f in found if keep(f[0])), default=(None, None))[1]  # noqa: E731
    return {"patch": pick(lambda n: n[:2] == nums[:2]), "minor": pick(lambda n: n[0] == nums[0]), "latest": max(found)[1]}


# --- rejestr (Docker Registry HTTP API v2) -------------------------------------------------------


class Registry:
    """HEAD manifestu i lista tagów z tokenem anonimowym albo z danymi z „Rejestrów” (Bearer/Basic wg WWW-Authenticate)."""

    def __init__(self, panel: Any, http: httpx.AsyncClient) -> None:
        self.panel, self.http = panel, http
        self.auth: dict[tuple[str, str], str] = {}
        self.gate = asyncio.Semaphore(PARALLEL)

    async def _send(self, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> httpx.Response:
        for attempt in range(RETRIES):
            try:
                response = await self.http.request(method, url, headers=headers, **kwargs)
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                    return response
            except httpx.TransportError:
                if attempt == RETRIES - 1:
                    raise
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise AssertionError("unreachable")

    def _credentials(self, ref: Ref) -> tuple[str, str] | None:
        from .registries import auth_for_image

        found = auth_for_image(self.panel, ref.image)
        return (found["username"], found["password"]) if found and found.get("password") else None

    async def _login(self, ref: Ref, challenge: str) -> str | None:
        scheme, _, rest = challenge.partition(" ")
        params = dict(re.findall(r'(\w+)="([^"]*)"', rest))
        credentials = self._credentials(ref)
        if scheme.lower() == "basic":
            return "Basic " + base64.b64encode(":".join(credentials).encode()).decode() if credentials else None
        if scheme.lower() != "bearer" or not params.get("realm"):
            return None
        query = {"service": params.get("service"), "scope": params.get("scope") or f"repository:{ref.repo}:pull"}
        response = await self._send("GET", params["realm"], {}, params={k: v for k, v in query.items() if v}, auth=credentials)
        response.raise_for_status()
        data = response.json()
        token = data.get("token") or data.get("access_token")
        return f"Bearer {token}" if token else None

    async def request(self, method: str, ref: Ref, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        async with self.gate:
            key = (ref.registry, ref.repo)
            headers = {**(headers or {}), **({"Authorization": self.auth[key]} if key in self.auth else {})}
            response = await self._send(method, url, headers)
            if response.status_code == 401 and response.headers.get("www-authenticate"):
                token = await self._login(ref, response.headers["www-authenticate"])
                if token:
                    self.auth[key] = token
                    response = await self._send(method, url, {**headers, "Authorization": token})
            return response

    async def digest(self, ref: Ref) -> str:
        url = f"https://{ref.registry}/v2/{ref.repo}/manifests/{ref.tag}"
        response = await self.request("HEAD", ref, url, {"Accept": ACCEPT})
        if response.status_code == 200 and response.headers.get("docker-content-digest"):
            return response.headers["docker-content-digest"]
        response = await self.request("GET", ref, url, {"Accept": ACCEPT})  # rejestr bez nagłówka przy HEAD
        response.raise_for_status()
        return response.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(response.content).hexdigest()

    async def tags(self, ref: Ref) -> list[str]:
        url, found = f"https://{ref.registry}/v2/{ref.repo}/tags/list?n=1000", []
        for _ in range(MAX_TAG_PAGES):
            response = await self.request("GET", ref, url)
            response.raise_for_status()
            found += response.json().get("tags") or []
            link = re.search(r"<([^>]+)>;\s*rel=\"next\"", response.headers.get("link", ""))
            if not link:
                break
            url = str(httpx.URL(url).join(link.group(1)))
        return found


async def remote(registry: Registry, ref: Ref) -> dict[str, Any]:
    """Digest tagu w rejestrze i (dla tagu z dokładną wersją) nowsze wersje."""
    try:
        out: dict[str, Any] = {"digest": await registry.digest(ref), "versions": None, "error": None}
        if pinned(ref.tag):
            out["versions"] = newer(ref.tag, await registry.tags(ref))
        return out
    except (httpx.HTTPError, ValueError) as exc:
        code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        return {"digest": None, "versions": None, "error": f"HTTP {code}" if code else (str(exc)[:200] or type(exc).__name__)}


# --- sprawdzanie --------------------------------------------------------------------------------


async def _digests(panel: Any, node: dict) -> tuple[dict | None, str | None]:
    try:
        return await asyncio.wait_for(panel.node_client(node).get("/api/images/digests", timeout=60), 65), None
    except RemoteError as exc:
        return None, t("appupd.node_too_old") if exc.status in (404, 405) else exc.detail
    except (httpx.HTTPError, asyncio.TimeoutError, OSError, LookupError):
        return None, t("vuln.node_offline")


def _app_images(panel: Any, node: dict, data: dict) -> dict[int, list[dict]]:
    """Kontenery aplikacji tego węzła (te z digestem z rejestru)."""
    from .uptime import app_containers
    from .vulns import _endpoint

    out: dict[int, list[dict]] = {}
    apps = [a for a in panel.db.fetchall("SELECT * FROM apps WHERE node_id = ?", [node["id"]]) if a["kind"] in UPDATABLE]
    for ep in data.get("endpoints") or []:
        items = [c for c in ep.get("containers") or [] if c.get("name")]
        for app in apps:
            if _endpoint(panel, app) != ep.get("id"):
                continue
            mine = [c for c in app_containers(items, app) if c.get("digests") and parse_ref(c.get("image") or "")]
            if mine:
                out.setdefault(app["id"], []).extend(mine)
    return out


def _summary(items: list[dict]) -> dict[str, Any]:
    """Skrót do ikony: strzałka i podpis (wersja albo „nowy build”)."""
    versions = [i["versions"]["minor"] or i["versions"]["latest"] for i in items if i.get("versions")]
    return {"kind": "version" if versions else "build", "label": versions[0] if versions else ""}


async def check(panel: Any, job: Any = None, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Sprawdza wszystkie aplikacje; zapisuje dostępne aktualizacje, wysyła powiadomienia i (tryb auto) aktualizuje."""
    log = job.log if job else (lambda _line: None)
    if running(panel):
        raise LookupError(t("appupd.already_running"))
    RUNNING.add(id(panel))
    started = time.time()
    result: dict[str, Any] = {"checked_at": started, "error": None, "nodes": {}, "images": 0, "updates": 0}
    config = settings(panel)
    try:
        nodes = [n for n in panel.db.all("nodes") if n["status"] == "paired" and n.get("secret")]
        answers = await asyncio.gather(*(_digests(panel, node) for node in nodes))
        per_app: dict[int, list[dict]] = {}
        checked: set[int] = set()
        for node, (data, error) in zip(nodes, answers):
            result["nodes"][str(node["id"])] = {"name": node["name"], "error": error}
            if data is None:
                log(t("vuln.log_node_error", node=node["name"], error=error))
                continue
            found = _app_images(panel, node, data)
            checked |= {a["id"] for a in panel.db.fetchall("SELECT id FROM apps WHERE node_id = ?", [node["id"]])}
            per_app.update(found)
            log(t("appupd.log_node", node=node["name"], apps=len(found)))
        refs = {c["image"]: parse_ref(c["image"]) for items in per_app.values() for c in items}
        result["images"] = len(refs)
        log(t("appupd.log_registry", count=len(refs)))
        headers = {"User-Agent": f"Deployra/{__version__}"}
        async with httpx.AsyncClient(timeout=20, headers=headers, transport=transport, follow_redirects=True) as http:
            registry = Registry(panel, http)
            answers = await asyncio.gather(*(remote(registry, ref) for ref in refs.values()))
        remote_of = dict(zip(refs, answers))
        failed = [f"{image}: {r['error']}" for image, r in remote_of.items() if r["error"]]
        for line in failed[:20]:
            log("⚠ " + line)
        updates: dict[int, list[dict]] = {}
        for app_id, containers in per_app.items():
            items = []
            for c in containers:
                info = remote_of.get(c["image"]) or {}
                build = bool(info.get("digest")) and info["digest"] not in c["digests"]
                if build or info.get("versions"):
                    items.append({"container": c["name"], "image": c["image"], "base": c.get("base", False), "build": build,
                                  "versions": info.get("versions")})  # fmt: skip
            if items:
                updates[app_id] = items
        result["updates"] = len(updates)
        result["apps"] = sorted(per_app)  # aplikacje, których obrazy naprawdę sprawdziliśmy („aktualna” tylko dla nich)
        events = store(panel, updates, checked)
        log(t("appupd.log_done", count=len(updates)))
        await notify(panel, events)
    except (httpx.HTTPError, ValueError) as exc:
        result["error"] = str(exc)[:300] or type(exc).__name__
        log(result["error"])
    finally:
        RUNNING.discard(id(panel))
        result["duration"] = round(time.time() - started, 1)
        panel.db.set_setting("app_updates_status", result)
        _badges.pop(id(panel), None)
    if not result["error"]:
        await auto_update(panel, config, job)
    return result


def store(panel: Any, updates: dict[int, list[dict]], checked: set[int]) -> list[tuple[dict, list[dict]]]:
    """Zapis dostępnych aktualizacji; zwraca (aplikacja, pozycje) tych, o których jeszcze nie powiadomiliśmy."""
    now = time.time()
    existing = {row["app_id"]: row for row in panel.db.fetchall("SELECT * FROM app_updates")}
    events = []
    for app_id, items in updates.items():
        signature = hashlib.sha1(repr(sorted((i["image"], i["build"], str(i["versions"])) for i in items)).encode()).hexdigest()[:16]
        row = existing.pop(app_id, None)
        if row:
            panel.db.update("app_updates", app_id, key="app_id", items=items, checked_at=now)
        else:
            panel.db.insert("app_updates", app_id=app_id, items=items, checked_at=now, notified="")
        if (row or {}).get("notified") != signature:
            panel.db.update("app_updates", app_id, key="app_id", notified=signature)
            app = panel.db.get("apps", app_id)
            if app:
                events.append((app, items))
    for app_id in existing:
        if app_id in checked or not panel.db.get("apps", app_id):  # węzeł odpowiedział, a aktualizacji już nie ma
            panel.db.delete("app_updates", app_id, key="app_id")
    return events


def short(item: dict) -> str:
    versions = item.get("versions") or {}
    target = versions.get("minor") or versions.get("latest")
    return t("appupd.short_version", version=target) if target else t("appupd.short_build")


def describe(item: dict) -> str:
    versions = item.get("versions") or {}
    target = versions.get("minor") or versions.get("latest")
    if target:
        return t("appupd.new_version", image=item["image"], version=target)
    return t("appupd.new_build", image=item["image"])


async def notify(panel: Any, events: list[tuple[dict, list[dict]]]) -> None:
    from . import alerts
    from .notify import Message

    config = settings(panel)
    for app, items in events:
        mode = policy(panel, app, config)
        if mode == "off" or (mode == "auto" and _auto_covers(app, items, config)):
            continue  # tryb auto: powiadomienie przyjdzie po aktualizacji; zostają tylko wersje, których sami nie zmienimy
        await alerts.event(panel, "app_update", Message(t("appupd.notify_title", name=app["name"]), "\n".join(describe(i) for i in items), "info",
                                                        data={"app": app["slug"], "images": [i["image"] for i in items]}))  # fmt: skip


# --- aktualizacja ---------------------------------------------------------------------------------


def available(panel: Any, app_id: int) -> list[dict]:
    row = panel.db.get("app_updates", app_id, key="app_id")
    return (row or {}).get("items") or []


def badges(panel: Any) -> dict[int, dict]:
    """app_id → skrót dla ikon (bez aplikacji z trybem „off”); odświeżane co kilka sekund, nie przy każdej ikonie."""
    cached = _badges.get(id(panel))
    if cached and time.time() - cached[0] < 10:
        return cached[1]
    config, out = settings(panel), {}
    rows = panel.db.fetchall("SELECT u.app_id, u.items, a.update_policy FROM app_updates u JOIN apps a ON a.id = u.app_id")
    for row in rows:
        mode = policy(panel, row, config)
        if mode != "off" and row.get("items"):
            out[row["app_id"]] = {**_summary(row["items"]), "auto": mode == "auto"}
    _badges[id(panel)] = (time.time(), out)
    return out


def _auto_covers(app: dict, items: list[dict], config: dict) -> bool:
    """Czy tryb auto zrobi wszystko sam (nowe buildy i — z `auto_patch` — wersje poprawkowe, gdy nowszych nie ma)?"""
    for item in items:
        versions = item.get("versions")
        if versions and not (config["auto_patch"] and app["kind"] in ("image", "compose") and versions.get("patch")
                             and versions["patch"] == versions.get("latest")):  # fmt: skip
            return False
    return True


def plan(panel: Any, app: dict, items: list[dict], config: dict, auto: bool) -> dict[str, str] | None:
    """Co zrobi aktualizacja: {stary obraz: nowy obraz} (pusty — tylko świeże pobranie) albo None, gdy nie ma czego robić."""
    changes: dict[str, str] = {}
    build = False
    for item in items:
        versions = item.get("versions") or {}
        target = versions.get("patch") if auto else (versions.get("minor") or versions.get("latest"))
        if target and (not auto or config["auto_patch"]) and app["kind"] in ("image", "compose"):
            changes[item["image"]] = with_tag(item["image"], target)
        elif item.get("build"):
            build = True
    return changes if changes or build else None


def apply_versions(panel: Any, app: dict, changes: dict[str, str]) -> None:
    """Nowy tag w zapisanej specyfikacji: obraz aplikacji albo dokładne wystąpienia w compose."""
    from . import deploy

    if not changes:
        return
    spec, options = deploy.load(panel, app)
    if spec.source.kind == "image" and spec.source.image in changes:
        spec = spec.model_copy(update={"source": spec.source.model_copy(update={"image": changes[spec.source.image]})})
    elif spec.source.kind == "compose" and spec.source.compose:
        compose = spec.source.compose
        for old, new in changes.items():
            compose = re.sub(rf"(image:\s*[\"']?){re.escape(old)}([\"']?\s*(?:#.*)?$)", rf"\g<1>{new}\g<2>", compose, flags=re.MULTILINE)
        spec = spec.model_copy(update={"source": spec.source.model_copy(update={"compose": compose})})
    panel.db.update("apps", app["id"], spec=deploy.store(panel, spec, options))


async def update_app(panel: Any, job: Any, app_id: int, changes: dict[str, str] | None = None) -> None:
    """Wdrożenie ze świeżo pobranym obrazem (i nowym tagiem, jeśli jest) — potem aplikacja jest aktualna."""
    from . import deploy

    app = panel.db.get("apps", app_id)
    if not app:
        raise LookupError(t("msg.no_app"))
    for old, new in (changes or {}).items():
        job.log(t("appupd.log_tag", old=old, new=new))
    apply_versions(panel, app, changes or {})
    await deploy.run(panel, job, app_id, refresh=True)  # after_deploy zdejmuje nowe buildy
    _keep(panel, app_id, [i for i in available(panel, app_id) if i["image"] not in (changes or {})])


def _keep(panel: Any, app_id: int, items: list[dict]) -> None:
    if items:
        panel.db.update("app_updates", app_id, key="app_id", items=items)
    else:
        panel.db.delete("app_updates", app_id, key="app_id")
    _badges.pop(id(panel), None)


def after_deploy(panel: Any, app: dict, refreshed: bool) -> None:
    """Wdrożenie obrazu/compose (albo z odświeżeniem) pobiera tag od nowa — nowy build przestaje być „dostępny”; nowe wersje zostają."""
    if refreshed or app["kind"] in ("image", "compose"):
        _keep(panel, app["id"], [{**i, "build": False} for i in available(panel, app["id"]) if i.get("versions")])


async def auto_update(panel: Any, config: dict, job: Any = None) -> list[str]:
    """Aplikacje w trybie „auto”: po kolei, w jednym zadaniu; nieudane zostają z alertem o nieudanym wdrożeniu."""
    from deployra_common.jobs import JobFailed

    from . import alerts
    from .notify import Message

    todo = []
    for row in panel.db.fetchall("SELECT * FROM app_updates"):
        app = panel.db.get("apps", row["app_id"])
        if app and app["status"] != "deploying" and policy(panel, app, config) == "auto":
            changes = plan(panel, app, row.get("items") or [], config, auto=True)
            if changes is not None:
                todo.append((app, changes))
    if not todo:
        return []
    done: list[str] = []

    async def work(run_job: Any) -> list[str]:
        for app, changes in todo:
            run_job.log(t("appupd.log_auto", name=app["name"]))
            try:
                await update_app(panel, run_job, app["id"], changes)
            except (JobFailed, LookupError, RemoteError, httpx.HTTPError) as exc:
                run_job.log(f"✖ {app['name']}: {getattr(exc, 'detail', None) or exc}")
                continue
            done.append(app["name"])
            text = ", ".join(f"{old} → {new}" for old, new in changes.items()) or t("appupd.fresh_image")
            await alerts.event(panel, "app_update", Message(t("appupd.updated_title", name=app["name"]), text, "ok", data={"app": app["slug"]}))
        return done

    if job:
        return await work(job)
    started = panel.jobs.start("deploy", t("appupd.auto_job"), work)
    logger.info(f"Automatyczne aktualizacje aplikacji: {len(todo)} (zadanie {started.id})")
    return [app["name"] for app, _ in todo]


async def loop(panel: Any) -> None:
    """Co TICK sprawdza, czy minął interwał (po nieudanej próbie — wcześniej)."""
    await asyncio.sleep(FIRST_CHECK)
    while True:
        i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)
        try:
            config, last = settings(panel), status(panel)
            wait = config["interval_hours"] * 3600
            if last.get("error"):
                wait = min(wait, RETRY_FAILED)
            if config["mode"] != "off" and not running(panel) and time.time() - float(last.get("checked_at") or 0) > wait:
                await check(panel)
        except Exception as exc:  # noqa: BLE001 — pętla nie może paść
            logger.error(f"Aktualizacje aplikacji: {exc}")
        await asyncio.sleep(TICK)
