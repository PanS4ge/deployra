"""Zużycie CPU i RAM kontenerów aplikacji — panel pyta co minutę (historia, alerty, ochrona przed przepełnieniem).

Statystyki pobieramy w trybie `one_shot` (bez dwusekundowego czekania Dockera na drugą próbkę),
a procent CPU liczymy z różnicy względem poprzedniego zapytania.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any

from . import docker_svc

ONEOFF = ("install", "transfer")


def _memory(stats: dict) -> int:
    """Pamięć jak w `docker stats`: użycie bez pamięci podręcznej plików."""
    mem = stats.get("memory_stats") or {}
    usage = int(mem.get("usage") or 0)
    extra = mem.get("stats") or {}
    cache = extra.get("inactive_file", extra.get("total_inactive_file", 0)) or 0
    return max(usage - int(cache), 0)


class UsageSampler:
    def __init__(self) -> None:
        self._prev: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deployra-usage")

    def _stats(self, client: Any, cid: str) -> dict:
        try:
            return client.api.stats(cid, stream=False, one_shot=True)
        except TypeError:  # starsze docker-py bez one_shot
            return client.api.stats(cid, stream=False)

    def _cpu(self, cid: str, stats: dict) -> float | None:
        cpu, pre = stats.get("cpu_stats") or {}, stats.get("precpu_stats") or {}
        total = int((cpu.get("cpu_usage") or {}).get("total_usage") or 0)
        system = int(cpu.get("system_cpu_usage") or 0)
        cores = int(cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1)
        with self._lock:
            previous = self._prev.get(cid)
            self._prev[cid] = (total, system)
        if pre.get("system_cpu_usage"):
            previous = (int((pre.get("cpu_usage") or {}).get("total_usage") or 0), int(pre["system_cpu_usage"]))
        if not previous or system <= previous[1]:
            return None
        return round(max(total - previous[0], 0) / (system - previous[1]) * cores * 100, 1)

    def collect(self, ctx: Any) -> dict[str, Any]:
        client = ctx.docker
        known = ctx.state.get("apps", {})
        compose_apps = {slug for slug, info in known.items() if info.get("kind") == "compose"}
        raw_containers = client.api.containers(all=True)

        def _sample_one(summary: dict) -> dict[str, Any] | None:
            labels = summary.get("Labels") or {}
            if labels.get(docker_svc.SYSTEM_LABEL) in ONEOFF:
                return None
            slug = labels.get(docker_svc.APP_LABEL)
            project = labels.get("com.docker.compose.project")
            if not slug and project in compose_apps:
                slug = project
            entry: dict[str, Any] = {
                "id": summary["Id"][:12],
                "name": (summary.get("Names") or ["/?"])[0].lstrip("/"),
                "app": slug,
                "state": summary.get("State"),
                "cpu": None,
                "mem": None,
            }
            if summary.get("State") == "running":
                try:
                    stats = self._stats(client, summary["Id"])
                    entry.update(cpu=self._cpu(summary["Id"], stats), mem=_memory(stats))
                except Exception:  # noqa: BLE001 — kontener mógł właśnie zniknąć
                    pass
            if slug:
                try:
                    info = client.api.inspect_container(summary["Id"])
                except Exception:  # noqa: BLE001
                    info = {}
                state = info.get("State") or {}
                entry.update(
                    limit=(info.get("HostConfig") or {}).get("Memory") or None,
                    restarts=int(info.get("RestartCount") or 0),
                    oom_at=state.get("FinishedAt") if state.get("OOMKilled") else None,
                    started_at=state.get("StartedAt"),
                )
            return entry

        if len(raw_containers) > 1:
            results = list(self._pool.map(_sample_one, raw_containers))
        else:
            results = [_sample_one(c) for c in raw_containers]

        containers = [c for c in results if c is not None]

        with self._lock:  # zapominamy kontenery, których już nie ma
            alive = {c["Id"] for c in raw_containers}
            self._prev = {k: v for k, v in self._prev.items() if k in alive}
        return {"ts": time.time(), "host": ctx.vitals.latest(), "apps": aggregate(containers), "containers": containers}


def aggregate(containers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Suma po aplikacji (stack compose to kilka kontenerów)."""
    apps: dict[str, dict[str, Any]] = {}
    for c in containers:
        if not c.get("app"):
            continue
        a = apps.setdefault(c["app"], {"cpu": 0.0, "mem": 0, "limit": 0, "restarts": 0, "oom_at": None, "running": 0, "containers": 0,
                                       "unlimited": False})
        a["containers"] += 1
        a["running"] += int(c.get("state") == "running")
        a["cpu"] = round(a["cpu"] + (c.get("cpu") or 0), 1)
        a["mem"] += c.get("mem") or 0
        a["limit"] += c.get("limit") or 0
        a["unlimited"] |= not c.get("limit")
        a["restarts"] += c.get("restarts") or 0
        if c.get("oom_at") and (not a["oom_at"] or c["oom_at"] > a["oom_at"]):
            a["oom_at"] = c["oom_at"]
    for a in apps.values():
        a["limit"] = None if a.pop("unlimited") else a["limit"]  # limit stacka tylko wtedy, gdy ma go każdy kontener
    return apps
