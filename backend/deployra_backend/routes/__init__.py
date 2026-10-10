"""Wspólne zależności routerów."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.sse import ServerSentEvent

from deployra_common.jobs import Job


async def signed(request: Request) -> str:
    return await request.app.state.ctx.guard(request)


async def signed_upload(request: Request) -> str:
    return await request.app.state.ctx.guard.upload(request)


def job_response(job: Job) -> dict[str, Any]:
    return {"job": job.snapshot()}


async def job_events(job: Job, start: int = 0):
    async for kind, payload in job.follow(start):
        if kind == "line":
            yield ServerSentEvent(raw_data=payload, event="line")
        else:
            yield ServerSentEvent(data=payload, event="end")
