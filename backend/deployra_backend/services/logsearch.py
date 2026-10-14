"""Przeszukiwanie logów kontenerów na żądanie (mini-ELK bez indeksu i bez stałego RAM-u).

Logi Dockera czytamy strumieniem, linia po linii, tylko z zadanego okresu. Linia pasuje, gdy pasują wszystkie
wzorce `must` i żaden z `exclude`. Zwracamy najnowsze trafienia i licznik trafień w przedziałach czasu
(do wykresu). Historia sięga tak daleko, jak trzyma ją Docker (aplikacje Deployry: 3 × 10 MB na kontener).
"""

from __future__ import annotations

import calendar
import re
import time
from typing import Any

from deployra_common.i18n import tr

from . import docker_svc

MAX_PATTERN = 200
SKIP = ("install", "transfer")


def compile_all(patterns: list[str]) -> list[re.Pattern[str]]:
    out = []
    for pattern in patterns[:10]:
        if not pattern or len(pattern) > MAX_PATTERN:
            raise ValueError(tr("node.logs.bad_query", max=MAX_PATTERN))
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(tr("node.logs.bad_regex", error=exc)) from None
    return out


def _epoch(stamp: str) -> float | None:
    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _lines(stream: Any):
    pending = ""
    for chunk in stream:
        pending += chunk.decode(errors="replace") if isinstance(chunk, bytes) else str(chunk)
        *lines, pending = pending.split("\n")
        yield from lines
    if pending:
        yield pending


def search(client: Any, must: list[re.Pattern[str]], exclude: list[re.Pattern[str]], since: float, until: float,
           containers: set[str] | None = None, apps: set[str] | None = None, limit: int = 200, buckets: int = 40,
           deadline: float = 20.0) -> dict[str, Any]:  # fmt: skip
    started = time.monotonic()
    step = max((until - since) / buckets, 1)
    counts = [0] * buckets
    hits: list[dict[str, Any]] = []
    total = scanned = 0
    timed_out = False
    for summary in client.api.containers(all=True):
        labels = summary.get("Labels") or {}
        if labels.get(docker_svc.SYSTEM_LABEL) in SKIP:
            continue
        name = (summary.get("Names") or ["/?"])[0].lstrip("/")
        app = labels.get(docker_svc.APP_LABEL) or labels.get("com.docker.compose.project")
        if (containers or apps) and name not in (containers or set()) and app not in (apps or set()):
            continue
        scanned += 1
        try:
            stream = client.api.logs(summary["Id"], stream=True, follow=False, timestamps=True, since=int(since), until=int(until) + 1)
        except Exception:
            continue
        try:
            for line in _lines(stream):
                stamp, _, message = line.partition(" ")
                if all(p.search(message) for p in must) and not any(p.search(message) for p in exclude):
                    ts = _epoch(stamp)
                    total += 1
                    if ts is not None and since <= ts <= until:
                        counts[min(int((ts - since) / step), buckets - 1)] += 1
                    hits.append({"container": name, "app": app, "ts": ts, "line": message[:2000]})
                    if len(hits) > limit * 4:  # trzymamy w pamięci tylko końcówkę — najnowsze linie
                        hits.sort(key=lambda h: h["ts"] or 0, reverse=True)
                        del hits[limit:]
                if time.monotonic() - started > deadline:
                    timed_out = True
                    break
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        if timed_out:
            break
    hits.sort(key=lambda h: h["ts"] or 0, reverse=True)
    return {"hits": hits[:limit], "total": total, "buckets": counts, "since": since, "step": step, "scanned": scanned, "timed_out": timed_out}
