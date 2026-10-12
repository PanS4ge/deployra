"""Zapytania do logów w stylu Kibany, w minimalnej wersji.

    błąd timeout              obie frazy (wielkość liter bez znaczenia)
    "connection refused"      dokładna fraza
    /5\\d\\d /                  wyrażenie regularne
    -healthcheck              bez linii z tym słowem
    app:sklep  container:redis  node:dom  level:error
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..i18n import t

LEVELS = {
    "error": r"\b(error|err|fatal|critical|crit|exception|panic|traceback)\b",
    "warn": r"\b(warn|warning)\b",
    "info": r"\b(info|notice)\b",
    "debug": r"\b(debug|trace)\b",
}
_TOKEN = re.compile(r'(-?)(?:(app|container|node|level):)?("([^"]*)"|/((?:\\/|[^/])+)/|(\S+))')


@dataclass
class LogQuery:
    must: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)


def parse(text: str) -> LogQuery:
    query = LogQuery()
    for negative, key, _, phrase, regex, word in _TOKEN.findall(text or ""):
        value = phrase if phrase else (regex.replace("\\/", "/") if regex else word)
        if not value:
            continue
        if key in ("app", "container", "node"):
            {"app": query.apps, "container": query.containers, "node": query.nodes}[key].append(value)
            continue
        if key == "level":
            pattern = LEVELS.get(value.lower())
            if not pattern:
                raise ValueError(t("logs.bad_level", levels=", ".join(LEVELS)))
        elif regex:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(t("logs.bad_regex", error=exc)) from None
            pattern = value
        else:
            pattern = re.escape(value)
        (query.exclude if negative else query.must).append(pattern)
    return query


def highlight(line: str, patterns: list[str]) -> str:
    """Linia z <mark> na dopasowaniach (escapowana — log może zawierać HTML)."""
    import html

    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, line, re.IGNORECASE):
            if match.end() > match.start():
                spans.append(match.span())
    if not spans:
        return html.escape(line)
    spans.sort()
    out, pos = [], 0
    for start, end in spans:
        if start < pos:
            start = pos
        if start >= end:
            continue
        out += [html.escape(line[pos:start]), "<mark>", html.escape(line[start:end]), "</mark>"]
        pos = end
    out.append(html.escape(line[pos:]))
    return "".join(out)
