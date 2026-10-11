"""Paczki językowe: w kodzie i szablonach są klucze, teksty leżą w `locales/<język>.json`.

    t("nodes.add.title")                    → „Dodaj węzeł” / “Add node”
    t("jobs.started", name="web")           → „Wdrażam web” — parametry jako {name}

Nowy język = skopiuj `en.json` jako `de.json` i przetłumacz wartości. Brakujący klucz
spada na język domyślny (polski), a gdy i tam go nie ma — pokazuje się sam klucz.

Bieżący język żyje w zmiennej kontekstowej: panel ustawia ją w middleware na każde
żądanie, a do bramki i węzłów przekazuje nagłówkiem Accept-Language — dzięki temu
zadania uruchomione z żądania (np. logi wdrożenia) mówią językiem użytkownika.
Bramka i backend mają wspólną paczkę „serwerową” w tym pakiecie (`tr`); panel
korzysta z niej jako z zapasowej (np. komunikaty walidacji ze `schemas.py`).
"""

from __future__ import annotations

import contextvars
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT = "pl"
current: contextvars.ContextVar[str] = contextvars.ContextVar("deployra_lang", default=DEFAULT)
_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_LANG = re.compile(r"^[a-z]{2,3}$")
SERVER_LOCALES = Path(__file__).parent / "locales"


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{name}."))
        else:
            out[name] = str(value)
    return out


def fill(text: str, params: dict[str, Any], escape: Any = None) -> str:
    """Podstawia {nazwa}; nieznane zostawia, pojedyncze klamry (np. w nginx) nie przeszkadzają."""
    if not params:
        return text

    def sub(match: re.Match[str]) -> str:
        if match.group(1) not in params:
            return match.group(0)
        value = params[match.group(1)]
        return str(escape(value) if escape else value)

    return _PARAM.sub(sub, text)


class Catalog:
    """Paczki z jednego lub kilku katalogów — pierwszy katalog ma pierwszeństwo przy tych samych kluczach."""

    def __init__(self, directories: Path | list[Path], default: str = DEFAULT) -> None:
        self.dirs = [Path(d) for d in (directories if isinstance(directories, list) else [directories])]
        self.default = default
        self._packs: dict[str, dict[str, str]] = {}

    @property
    def languages(self) -> list[str]:
        return sorted({p.stem for d in self.dirs for p in d.glob("*.json") if _LANG.match(p.stem)})

    def pack(self, lang: str) -> dict[str, str]:
        if lang not in self._packs:
            merged: dict[str, str] = {}
            if _LANG.match(lang):
                for directory in reversed(self.dirs):
                    path = directory / f"{lang}.json"
                    if path.is_file():
                        merged.update(_flatten(json.loads(path.read_text(encoding="utf-8"))))
            self._packs[lang] = merged
        return self._packs[lang]

    def names(self) -> dict[str, str]:
        """{kod: nazwa własna języka} — do przełącznika."""
        return {lang: self.pack(lang).get("_language", lang) for lang in self.languages}

    def raw(self, key: str, lang: str | None = None) -> str:
        lang = lang or current.get()
        text = self.pack(lang).get(key)
        if text is None and lang != self.default:
            text = self.pack(self.default).get(key)
        return key if text is None else text

    def __call__(self, key: str, /, **params: Any) -> str:
        return fill(self.raw(key), params)

    def supported(self, lang: str | None) -> str | None:
        lang = (lang or "").strip().lower()[:3]
        return lang if lang in self.languages else None

    def negotiate(self, header: str | None) -> str | None:
        """Pierwszy obsługiwany język z nagłówka Accept-Language (bez wag — kolejność wystarcza)."""
        for part in (header or "").split(","):
            found = self.supported(part.split(";")[0].strip().split("-")[0])
            if found:
                return found
        return None


server = Catalog(SERVER_LOCALES)


def tr(key: str, /, **params: Any) -> str:
    """Tekst z paczki serwerowej (bramka, backend, wspólny kod) w bieżącym języku."""
    return server(key, **params)


def default_language() -> str:
    """Język logów i zadań bez żądania (DEPLOYRA_LANG, domyślnie polski)."""
    return server.supported(os.environ.get("DEPLOYRA_LANG")) or DEFAULT


def set_language(lang: str) -> contextvars.Token[str]:
    return current.set(lang)


def language_middleware(app: Any) -> None:
    """Middleware FastAPI: język odpowiedzi i zadań z nagłówka Accept-Language (wysyła go panel)."""

    @app.middleware("http")
    async def _lang(request: Any, call_next: Any) -> Any:
        current.set(server.negotiate(request.headers.get("accept-language")) or default_language())
        return await call_next(request)
