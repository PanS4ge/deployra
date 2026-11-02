"""Kody błędów do zgłoszeń: pod błędem zadania ląduje jedna linia `⌁ DPL-… · skąd · gdzie w kodzie · kontekst`.

Linię da się wkleić w zgłoszenie (albo rozmowę) i od razu wiadomo, który komponent, która wersja i które miejsce
w kodzie zgłosiło błąd, plus dane, których nie widać w samym komunikacie (port, kontener, obraz…). Spis kodów
i co zwykle znaczą: docs/errors.md.
"""

from __future__ import annotations

import os
import traceback
from types import TracebackType
from typing import Any

from .jobs import JobFailed

MARK = "⌁"


class CodedError(JobFailed):
    """Czytelny błąd zadania z kodem i kontekstem (`CodedError("port zajęty", code="DPL-PORT", port=8080)`)."""

    code = "DPL-JOB"

    def __init__(self, message: str = "", *, code: str | None = None, **context: Any) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.context = {k: v for k, v in context.items() if v not in (None, "", [], {})}


def code_of(exc: BaseException) -> str:
    """Kod błędu: własny (`code`), z odpowiedzi węzła (`RemoteError` → DPL-NODE-<status>) albo z rodzaju wyjątku."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.startswith("DPL-"):
        return code
    status = getattr(exc, "status", None)
    if isinstance(status, int) and type(exc).__name__ == "RemoteError":
        return f"DPL-NODE-{status}"
    module = type(exc).__module__ or ""
    if module.startswith(("httpx", "httpcore")) or isinstance(exc, (ConnectionError, TimeoutError)):
        return "DPL-NET"
    if isinstance(exc, JobFailed):
        return "DPL-JOB"
    return "DPL-BUG"


def _where(tb: TracebackType | None) -> str | None:
    """Ostatnie miejsce w kodzie Deployry, przez które przeszedł wyjątek (`deploy.py:131`)."""
    found = None
    for frame in traceback.extract_tb(tb):
        path = frame.filename.replace("\\", "/")
        if "/deployra_" in path and not path.endswith(("/jobs.py", "/errors.py")):
            found = f"{os.path.basename(path)}:{frame.lineno}"
    return found


def diag_line(exc: BaseException, origin: str = "", kind: str = "") -> str:
    """`⌁ DPL-PORT · node 1.8.1 · deploy · ports.py:92 · port=8080 holder=errpage`"""
    parts = [f"{MARK} {code_of(exc)}"]
    parts += [p for p in (origin, kind) if p]
    where = _where(exc.__traceback__)
    cause = exc.__cause__ or exc.__context__
    cause_where = _where(cause.__traceback__) if cause is not None else None
    if where:
        parts.append(where)
    if cause is not None and cause_where != where:  # błąd opakowany w czytelny komunikat — skąd naprawdę przyszedł
        parts.append(f"← {type(cause).__name__}" + (f"@{cause_where}" if cause_where else ""))
    if code_of(exc) == "DPL-BUG":
        parts.append(type(exc).__name__)
    context = getattr(exc, "context", None) or {}
    if context:
        parts.append(" ".join(f"{k}={_short(v)}" for k, v in context.items()))
    return " · ".join(parts)


def _short(value: Any) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= 120 else text[:117] + "…"
