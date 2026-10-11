"""Język panelu: paczki w `locales/`, wybór per żądanie (użytkownik → ciasteczko → domyślny → przeglądarka)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from markupsafe import Markup, escape

from deployra_common.i18n import DEFAULT, SERVER_LOCALES, Catalog, current, fill

# własne teksty panelu, a w zapasie paczka serwerowa (komunikaty walidacji i wspólnego kodu)
catalog = Catalog([Path(__file__).parent / "locales", SERVER_LOCALES])
LANG_COOKIE = "dpl_lang"


def t(key: str, /, **params: Any) -> str:
    """Tekst do kodu Pythona (toasty, błędy, logi zadań) — zwykły str."""
    return catalog(key, **params)


def tm(key: str, /, **params: Any) -> Markup:
    """Tekst do szablonów: paczka może zawierać prosty HTML (<b>, <code>, <a>), parametry są escapowane."""
    return Markup(fill(catalog.raw(key), params, escape))


def lang() -> str:
    return current.get()


def resolve(user: dict[str, Any] | None, cookie: str | None, default: str | None, accept: str | None) -> str:
    for candidate in ((user or {}).get("lang"), cookie, default):
        found = catalog.supported(candidate)
        if found:
            return found
    return catalog.negotiate(accept) or DEFAULT
