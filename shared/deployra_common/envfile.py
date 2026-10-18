"""Zmienne środowiskowe aplikacji: format pliku `.env`, zakresy i łączenie warstw.

Zakres mówi, dokąd zmienna trafia:
- `runtime` — do działającego kontenera (domyślny, jak dotąd),
- `build`   — tylko na etapie budowania: argumenty `docker build` i instalacja zależności,
- `both`    — wszędzie.

W pliku `.env` zakres zapisuje się znacznikiem w linii tuż nad zmienną: `# @build`.
Wartości w cudzysławach mogą mieć wiele linii (`"linia1` / `linia2"`), a w `"…"` działają
escape’y JSON (`\\n`, `\\"`), tak jak w zapisie, który panel zwraca do edytora.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from .i18n import tr

SCOPES = ("runtime", "build", "both")
RUNTIME_SCOPES = ("runtime", "both")
BUILD_SCOPES = ("build", "both")
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MARK = re.compile(r"^#\s*@(\w+)\s*$")
_COMMENT = re.compile(r"(^|\s)#.*$")
_NEEDS_QUOTES = re.compile(r"[\n\r\"'#]")

Layer = tuple[dict[str, str], dict[str, str]]  # (zmienne, zakresy — tylko te inne niż runtime)


class EnvFileError(ValueError):
    """Zła linia w pliku `.env` (komunikat już przetłumaczony)."""


def scoped(env: dict[str, str], scopes: dict[str, str] | None, wanted: Sequence[str]) -> dict[str, str]:
    """Zmienne o zakresie z `wanted` — kolejność i wartości bez zmian."""
    scopes = scopes or {}
    return {key: value for key, value in env.items() if scopes.get(key, "runtime") in wanted}


def merge(layers: Sequence[Layer]) -> Layer:
    """Warstwy w kolejności: każda kolejna nadpisuje klucze poprzednich (razem z zakresem)."""
    env: dict[str, str] = {}
    scopes: dict[str, str] = {}
    for layer_env, layer_scopes in layers:
        for key, value in layer_env.items():
            env[key] = value
            scope = (layer_scopes or {}).get(key, "runtime")
            if scope == "runtime":
                scopes.pop(key, None)
            else:
                scopes[key] = scope
    return env, scopes


def parse(text: str) -> Layer:
    """Plik `.env` → (zmienne, zakresy). Obsługuje `export`, komentarze `#`, cudzysłowy i wieloliniowe wartości."""
    env: dict[str, str] = {}
    scopes: dict[str, str] = {}
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    mark: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if line.startswith("#"):
            found = _MARK.match(line)
            if found:
                if found.group(1) not in SCOPES:
                    raise EnvFileError(tr("srv.schema.bad_env_scope", scope=found.group(1)))
                mark = found.group(1)
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, rest = line.partition("=")
        key = key.strip()
        if not sep or not KEY.match(key):
            raise EnvFileError(tr("srv.schema.bad_env_line", line=line))
        rest = rest.lstrip()
        if rest[:1] in ('"', "'"):
            value, i = _quoted(rest, lines, i)
        else:
            value = _COMMENT.sub("", rest).strip()
        env[key] = value
        scope = mark or "runtime"
        mark = None
        if scope == "runtime":
            scopes.pop(key, None)
        else:
            scopes[key] = scope
    return env, scopes


def dump(env: dict[str, str], scopes: dict[str, str] | None = None) -> str:
    """(zmienne, zakresy) → tekst `.env`; wartości z niebezpiecznymi znakami idą w cudzysłowie JSON."""
    lines: list[str] = []
    for key, value in env.items():
        scope = (scopes or {}).get(key, "runtime")
        if scope != "runtime":
            lines.append(f"# @{scope}")
        quoted = _NEEDS_QUOTES.search(value) or value != value.strip()
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False) if quoted else value}")
    return "\n".join(lines) + ("\n" if lines else "")


def _quoted(rest: str, lines: list[str], i: int) -> tuple[str, int]:
    """Wartość w cudzysłowie, która może sięgać kolejnych linii; zwraca (wartość, indeks następnej linii)."""
    quote, buf = rest[0], rest[1:]
    while (end := _closing(buf, quote)) is None:
        if i >= len(lines):
            raise EnvFileError(tr("srv.schema.bad_env_quote", line=rest[:80]))
        buf += "\n" + lines[i]
        i += 1
    body = buf[:end]
    return (body if quote == "'" else _unescape(body)), i


def _closing(text: str, quote: str) -> int | None:
    """Indeks zamykającego cudzysłowu; w `"…"` ukośnik escape’uje następny znak."""
    skip = False
    for index, char in enumerate(text):
        if skip:
            skip = False
        elif quote == '"' and char == "\\":
            skip = True
        elif char == quote:
            return index
    return None


def _unescape(body: str) -> str:
    try:  # wieloliniowa wartość: surowe nowe linie zamieniamy na escape’y, żeby JSON je przyjął
        return json.loads('"' + body.replace("\n", "\\n").replace("\r", "\\r") + '"', strict=False)
    except ValueError:
        return body
