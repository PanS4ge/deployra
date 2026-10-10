"""Przekazywanie strumieni SSE z węzłów/bramki (logi, zadania) do panelu i przeglądarki."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from deployra_common.client import SignedClient
from deployra_common.jobs import Job, JobFailed


async def iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Parsuje strumień text/event-stream → (zdarzenie, dane)."""
    event, data = "message", []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:][1:] if line[5:].startswith(" ") else line[5:])


async def follow_remote_job(client: SignedClient, job_id: str, panel_job: Job, prefix: str = "") -> dict[str, Any]:
    """Przepisuje log zdalnego zadania do zadania panelu; rzuca JobFailed, jeśli zdalne się nie udało."""
    async with client.stream("GET", f"/api/jobs/{job_id}/stream") as response:
        async for event, data in iter_sse(response):
            if event == "line":
                panel_job.log(f"{prefix}{data}")
            elif event == "end":
                snapshot = json.loads(data)
                if snapshot.get("status") != "ok":
                    raise JobFailed(snapshot.get("error") or "zdalne zadanie nie powiodło się")
                return snapshot
    raise JobFailed("zerwane połączenie ze zdalnym zadaniem")


async def start_and_follow(client: SignedClient, path: str, body: Any, panel_job: Job, prefix: str = "") -> dict[str, Any]:
    started = await client.post(path, json=body)
    return await follow_remote_job(client, started["job"]["id"], panel_job, prefix)
