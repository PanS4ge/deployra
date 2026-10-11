"""Pluginy panelu: katalog z `plugin.json` i (opcjonalnie) kodem Pythona.

Plugin może:
- dodać strony (router FastAPI pod /plugins/<id>/…) i pozycje w menu,
- reagować na zdarzenia (wdrożenie, awaria monitora, nowa wersja, każde powiadomienie…),
- dorzucić szablony aplikacji do kreatora („Z szablonu”),
- dostarczyć paczki językowe — także zupełnie nowy język,
- mieć własne szablony Jinja (`templates/` → "plugins/<id>/…") i pliki (`static/` → /plugins/<id>/static/…).

Kod pluginu działa w procesie panelu z pełnymi uprawnieniami, więc pluginy wgrane przez
użytkownika są domyślnie wyłączone, a kod wyłączonego pluginu nigdy się nie wykonuje.
Wyłączenie działa od razu (strony → 404, zdarzenia i menu znikają); pełne wyładowanie
modułu z pamięci wymaga restartu panelu.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import secrets
import shutil
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from jinja2 import BaseLoader, TemplateNotFound
from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator

from deployra_common.updates import is_newer

from . import __version__
from .i18n import catalog, lang, t

MANIFEST = "plugin.json"
PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
EVENTS = ("app.deployed", "app.failed", "app.removed", "monitor.down", "monitor.up", "node.paired", "update.available", "notification")
BUILTIN_DIR = Path(__file__).parent / "plugins_builtin"
MAX_ZIP = 20 * 1024 * 1024


class AppTemplate(BaseModel):
    """Gotowa aplikacja do kreatora. `${RANDOM}` w env/compose zamienia się na losowy sekret."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    name: str | dict[str, str]
    description: str | dict[str, str] = ""
    icon: str = Field(default="box", pattern=r"^[a-z0-9-]{1,40}$")
    kind: Literal["image", "compose"] = "image"
    image: str = ""
    container_port: int | None = Field(default=None, ge=1, le=65535)
    compose: str = ""
    expose_service: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    volumes: list[str] = Field(default_factory=list)
    memory_mb: int | None = Field(default=None, ge=16)
    slug: str = ""


class Manifest(BaseModel):
    id: str
    name: str | dict[str, str]
    version: str = "0.1.0"
    description: str | dict[str, str] = ""
    author: str = ""
    homepage: str = ""
    requires: str = ""  # najniższa wersja Deployry, np. "1.1.0"
    entry: str | None = None  # plik Pythona z funkcją setup(api)
    app_templates: list[AppTemplate] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not PLUGIN_ID.match(value):
            raise ValueError("a-z 0-9 - _ (2–40)")
        return value

    @field_validator("entry")
    @classmethod
    def _entry(cls, value: str | None) -> str | None:
        if value and (not value.endswith(".py") or "/" in value or "\\" in value or value.startswith(".")):
            raise ValueError("<file>.py")
        return value


def problem(exc: Exception) -> str:
    """Krótki opis błędu manifestu do pokazania w panelu."""
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:300]
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


def localized(value: str | dict[str, str]) -> str:
    """Tekst z manifestu: zwykły napis albo {"pl": …, "en": …}."""
    if isinstance(value, dict):
        return value.get(lang()) or value.get("en") or next(iter(value.values()), "")
    return value


@dataclass
class Plugin:
    id: str
    path: Path
    builtin: bool
    manifest: Manifest | None = None
    error: str | None = None
    enabled: bool = False
    loaded: bool = False
    module: Any = None
    nav: list[dict[str, Any]] = field(default_factory=list)
    handlers: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    templates: list[AppTemplate] = field(default_factory=list)

    @property
    def name(self) -> str:
        return localized(self.manifest.name) if self.manifest else self.id

    @property
    def description(self) -> str:
        return localized(self.manifest.description) if self.manifest else ""

    @property
    def capabilities(self) -> list[str]:
        caps = []
        if self.manifest and self.manifest.entry:
            caps.append("code")
        if (self.path / "locales").is_dir():
            caps.append("locales")
        if self.app_templates:
            caps.append("templates")
        if (self.path / "templates").is_dir():
            caps.append("pages")
        return caps

    @property
    def app_templates(self) -> list[AppTemplate]:
        return [*(self.manifest.app_templates if self.manifest else []), *self.templates]


class PluginAPI:
    """To dostaje funkcja `setup(api)` pluginu."""

    def __init__(self, manager: PluginManager, plugin: Plugin) -> None:
        self._manager, self._plugin = manager, plugin
        self.id = plugin.id
        self.dir = plugin.path
        self.panel = manager.panel
        self.logger = logger.bind(plugin=plugin.id)

    def router(self) -> APIRouter:
        """Router pod /plugins/<id>; wymaga zalogowania, po wyłączeniu pluginu zwraca 404."""
        from .deps import current_user

        def guard() -> None:
            if not self._plugin.enabled:
                raise HTTPException(404)

        router = APIRouter(prefix=f"/plugins/{self.id}", dependencies=[Depends(current_user), Depends(guard)])
        self._manager._routers.append((self._plugin, router))
        return router

    def add_nav(self, label: str | dict[str, str], href: str, icon: str = "puzzle") -> None:
        """Pozycja w menu. `label`: klucz z paczki pluginu ("title" → plugin.<id>.title), tekst albo {"pl": …, "en": …}."""
        self._plugin.nav.append({"label": label, "href": href, "icon": icon})

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        if event not in EVENTS and event != "*":
            raise ValueError(f"nieznane zdarzenie {event!r}; dostępne: {', '.join(EVENTS)}")
        self._plugin.handlers.setdefault(event, []).append(handler)

    def add_app_template(self, template: dict[str, Any]) -> None:
        self._plugin.templates.append(AppTemplate.model_validate(template))

    def setting(self, key: str, default: Any = None) -> Any:
        return self.panel.db.setting(f"plugin.{self.id}.{key}", default)

    def set_setting(self, key: str, value: Any) -> None:
        self.panel.db.set_setting(f"plugin.{self.id}.{key}", value)

    def t(self, key: str, /, **params: Any) -> str:
        return t(f"plugin.{self.id}.{key}", **params)

    def render(self, request: Request, template: str, **context: Any) -> Any:
        from .web import render

        return render(request, f"plugins/{self.id}/{template}", **context)

    def notify(self, message: str, level: str = "info") -> None:
        self.panel.notify(message, level)


class PluginTemplateLoader(BaseLoader):
    """Szablony włączonych pluginów jako "plugins/<id>/<plik>" — mogą rozszerzać base.html."""

    def __init__(self, manager: PluginManager) -> None:
        self.manager = manager

    def get_source(self, environment: Any, template: str) -> tuple[str, str, Callable[[], bool]]:
        parts = template.split("/", 2)
        if len(parts) != 3 or parts[0] != "plugins":
            raise TemplateNotFound(template)
        plugin = self.manager.plugins.get(parts[1])
        if not plugin or not plugin.enabled:
            raise TemplateNotFound(template)
        root = (plugin.path / "templates").resolve()
        path = (root / parts[2]).resolve()
        if root not in path.parents or not path.is_file():
            raise TemplateNotFound(template)
        mtime = path.stat().st_mtime
        return path.read_text(encoding="utf-8"), str(path), lambda: path.stat().st_mtime == mtime


class PluginManager:
    def __init__(self, panel: Any, user_dir: Path, builtin_dir: Path = BUILTIN_DIR) -> None:
        self.panel = panel
        self.user_dir = user_dir
        self.builtin_dir = builtin_dir
        self.plugins: dict[str, Plugin] = {}
        self._routers: list[tuple[Plugin, APIRouter]] = []
        self._app: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.discover()

    # --- odkrywanie i stan ---------------------------------------------------------------

    def _state(self) -> dict[str, list[str]]:
        state = self.panel.db.setting("plugins") or {}
        return {"enabled": list(state.get("enabled", [])), "disabled": list(state.get("disabled", []))}

    def _save_state(self, plugin_id: str, enabled: bool) -> None:
        state = self._state()
        state["enabled"] = [i for i in state["enabled"] if i != plugin_id] + ([plugin_id] if enabled else [])
        state["disabled"] = [i for i in state["disabled"] if i != plugin_id] + ([] if enabled else [plugin_id])
        self.panel.db.set_setting("plugins", state)

    def discover(self) -> None:
        state = self._state()
        found: dict[str, Plugin] = {}
        for builtin, root in ((True, self.builtin_dir), (False, self.user_dir)):
            if not root.is_dir():
                continue
            for path in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
                plugin = self.plugins.get(path.name) or Plugin(id=path.name, path=path, builtin=builtin)
                try:
                    manifest = Manifest.model_validate(json.loads((path / MANIFEST).read_text(encoding="utf-8")))
                    if manifest.id != path.name:
                        raise ValueError(t("plugins.id_mismatch", id=manifest.id, dir=path.name))
                    if manifest.requires and is_newer(manifest.requires, __version__):
                        raise ValueError(t("plugins.requires", version=manifest.requires, current=__version__))
                    plugin.manifest, plugin.error = manifest, (plugin.error if plugin.loaded else None)
                except (OSError, ValueError, ValidationError) as exc:
                    plugin.manifest, plugin.error = None, problem(exc)
                default = builtin and plugin.id not in state["disabled"]
                plugin.enabled = plugin.manifest is not None and (plugin.id in state["enabled"] or default)
                found.setdefault(plugin.id, plugin)
        self.plugins = found
        self._refresh_locales()

    def _refresh_locales(self) -> None:
        """Paczki językowe włączonych pluginów: po paczkach panelu, przed paczką serwerową."""
        own, server = catalog.dirs[0], catalog.dirs[-1]
        extra = [p.path / "locales" for p in self.plugins.values() if p.enabled and (p.path / "locales").is_dir()]
        catalog.dirs[:] = [own, *extra, server]
        catalog._packs.clear()

    # --- ładowanie kodu ---------------------------------------------------------------------

    def start(self, app: Any) -> None:
        self._app = app
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        for plugin in self.plugins.values():
            if plugin.enabled:
                self._load(plugin)

    def _load(self, plugin: Plugin) -> None:
        if plugin.loaded or plugin.manifest is None:
            return
        plugin.loaded = True
        if not plugin.manifest.entry:
            return
        try:
            name = f"deployra_plugin_{plugin.id.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(name, plugin.path / plugin.manifest.entry)
            if spec is None or spec.loader is None:
                raise ImportError(plugin.manifest.entry)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise AttributeError(t("plugins.no_setup"))
            plugin.module = module
            setup(PluginAPI(self, plugin))
            if self._app is not None:
                for owner, router in self._routers:
                    if owner is plugin:
                        self._app.include_router(router)
            logger.info(f"Plugin {plugin.id} {plugin.manifest.version} załadowany")
        except Exception as exc:  # noqa: BLE001 — zepsuty plugin nie może wywrócić panelu
            # sprzątamy to, co setup zdążył zarejestrować — po poprawce można włączyć jeszcze raz
            plugin.error = f"{type(exc).__name__}: {exc}"[:300]
            plugin.enabled, plugin.loaded, plugin.module = False, False, None
            plugin.nav, plugin.handlers, plugin.templates = [], {}, []
            self._routers = [(owner, router) for owner, router in self._routers if owner is not plugin]
            logger.error(f"Plugin {plugin.id}: {plugin.error}")

    def enable(self, plugin_id: str) -> Plugin:
        plugin = self._get(plugin_id)
        if plugin.manifest is None:
            raise ValueError(plugin.error or plugin_id)
        plugin.enabled, plugin.error = True, None
        self._save_state(plugin_id, True)
        self._refresh_locales()
        self._load(plugin)
        if plugin.error:
            self._save_state(plugin_id, False)
        return plugin

    def disable(self, plugin_id: str) -> Plugin:
        plugin = self._get(plugin_id)
        plugin.enabled = False
        self._save_state(plugin_id, False)
        self._refresh_locales()
        return plugin

    def _get(self, plugin_id: str) -> Plugin:
        plugin = self.plugins.get(plugin_id)
        if plugin is None:
            raise LookupError(t("plugins.not_found"))
        return plugin

    # --- instalacja z ZIP ---------------------------------------------------------------------

    def install_zip(self, archive: Path) -> Plugin:
        """Rozpakowuje plugin do katalogu użytkownika (bez wykonywania kodu — włącza się osobno)."""
        if archive.stat().st_size > MAX_ZIP:
            raise ValueError(t("plugins.zip_too_big"))
        try:
            with zipfile.ZipFile(archive) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                manifest_names = [n for n in names if n.split("/")[-1] == MANIFEST and n.count("/") <= 1]
                if len(manifest_names) != 1:
                    raise ValueError(t("plugins.zip_no_manifest"))
                prefix = manifest_names[0][: -len(MANIFEST)]
                manifest = Manifest.model_validate(json.loads(zf.read(manifest_names[0]).decode("utf-8")))
                target = self.user_dir / manifest.id
                if manifest.id in self.plugins and self.plugins[manifest.id].builtin:
                    raise ValueError(t("plugins.zip_builtin", id=manifest.id))
                staging = self.user_dir / f".{manifest.id}-{secrets.token_hex(4)}"
                staging.mkdir(parents=True)
                try:
                    for info in zf.infolist():
                        if info.is_dir() or not info.filename.startswith(prefix):
                            continue
                        rel = info.filename[len(prefix):]
                        dest = (staging / rel).resolve()
                        if staging.resolve() not in dest.parents or (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError(t("plugins.zip_unsafe", name=info.filename))
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(info))
                    was_enabled = manifest.id in self.plugins and self.plugins[manifest.id].enabled
                    if target.exists():
                        shutil.rmtree(target)
                    staging.rename(target)
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
        except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(t("plugins.zip_bad", error=exc)) from None
        except ValidationError as exc:
            raise ValueError(t("plugins.zip_bad", error=problem(exc))) from None
        self.discover()
        plugin = self.plugins[manifest.id]
        if was_enabled and plugin.loaded:
            plugin.error = t("plugins.restart_needed")
        return plugin

    def remove(self, plugin_id: str) -> None:
        plugin = self._get(plugin_id)
        if plugin.builtin:
            raise ValueError(t("plugins.builtin_remove"))
        self.disable(plugin_id)
        shutil.rmtree(plugin.path, ignore_errors=True)
        self.plugins.pop(plugin_id, None)

    # --- zdarzenia ------------------------------------------------------------------------------

    def emit(self, event: str, **data: Any) -> None:
        """Wywołuje obsługę zdarzenia we włączonych pluginach — w tle, błędy tylko do logu."""
        for plugin in list(self.plugins.values()):
            if not plugin.enabled:
                continue
            for handler in plugin.handlers.get(event, []) + plugin.handlers.get("*", []):
                self._dispatch(plugin, handler, event, data)

    def _dispatch(self, plugin: Plugin, handler: Callable[..., Any], event: str, data: dict[str, Any]) -> None:
        async def call() -> None:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(event, data)
                else:
                    await asyncio.to_thread(handler, event, data)
            except Exception as exc:  # noqa: BLE001 — błąd pluginu nie może przerwać panelu
                logger.error(f"Plugin {plugin.id} ({event}): {type(exc).__name__}: {exc}")

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            running.create_task(call())
        elif self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(call(), self._loop)

    # --- dla interfejsu -----------------------------------------------------------------------

    def nav_items(self) -> list[dict[str, str]]:
        def label(plugin: Plugin, value: str | dict[str, str]) -> str:
            if isinstance(value, dict):
                return localized(value)
            key = f"plugin.{plugin.id}.{value}"
            text = catalog.raw(key)
            return value if text == key else text

        return [{**item, "label": label(p, item["label"])} for p in self.plugins.values() if p.enabled and p.loaded for item in p.nav]

    def app_templates(self) -> list[dict[str, Any]]:
        out = []
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            for tpl in plugin.app_templates:
                out.append({"key": f"{plugin.id}:{tpl.id}", "plugin": plugin.name, "name": localized(tpl.name),
                            "description": localized(tpl.description), "icon": tpl.icon, "kind": tpl.kind})  # fmt: skip
        return sorted(out, key=lambda item: item["name"].lower())

    def app_template(self, key: str) -> dict[str, Any] | None:
        """Szablon gotowy do wypełnienia kreatora (z podstawionymi sekretami ${RANDOM})."""
        plugin_id, _, tpl_id = key.partition(":")
        plugin = self.plugins.get(plugin_id)
        if not plugin or not plugin.enabled:
            return None
        tpl = next((x for x in plugin.app_templates if x.id == tpl_id), None)
        if tpl is None:
            return None

        def fill(text: str) -> str:
            return re.sub(r"\$\{RANDOM\}", lambda _: secrets.token_urlsafe(24), text)

        return {
            "kind": tpl.kind, "name": localized(tpl.name), "slug": tpl.slug or tpl.id, "container_port": tpl.container_port,
            "source": {"kind": tpl.kind, "image": tpl.image, "compose": fill(tpl.compose), "expose_service": tpl.expose_service},
            "env": "".join(f"{k}={fill(v)}\n" for k, v in tpl.env.items()), "volumes": "".join(f"{v}\n" for v in tpl.volumes),
            "memory_mb": tpl.memory_mb,
        }  # fmt: skip


def static_route(manager: PluginManager) -> APIRouter:
    """/plugins/<id>/static/<plik> — pliki statyczne włączonych pluginów (po zalogowaniu)."""
    from .deps import current_user

    router = APIRouter(dependencies=[Depends(current_user)])

    @router.get("/plugins/{plugin_id}/static/{path:path}")
    def plugin_static(plugin_id: str, path: str) -> FileResponse:
        plugin = manager.plugins.get(plugin_id)
        if not plugin or not plugin.enabled:
            raise HTTPException(404)
        root = (plugin.path / "static").resolve()
        file = (root / path).resolve()
        if root not in file.parents or not file.is_file():
            raise HTTPException(404)
        return FileResponse(file)

    return router

