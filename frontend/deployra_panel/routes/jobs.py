"""Zadania panelu (deploy, certyfikaty…) z logiem na żywo."""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from deployra_common.jobs import Job

from ..context import Panel, get_panel
from ..deps import current_user
from ..i18n import t
from ..web import render

router = APIRouter(dependencies=[Depends(current_user)])


def line_html(text: str) -> str:
    cls = "l"
    stripped = text.lstrip()
    if stripped.startswith("$ "):
        cls += " cmd"
    elif stripped.startswith(("✖", "⚠", "ERROR", "error:", "Error")):
        cls += " err"
    elif stripped.startswith(("✔", "✨")):
        cls += " ok"
    return f'<span class="{cls}">{html.escape(text) or "&nbsp;"}</span>'


def end_html(job: Job) -> str:
    if job.status == "ok":
        return f'<span class="badge ok"><i class="bi bi-check2-circle"></i> {t("jobs.finished")}</span>'
    return f'<span class="badge err"><i class="bi bi-x-octagon"></i> {t("jobs.error")}</span> <span class="small muted">{html.escape(job.error or "")}</span>'


async def job_sse(job: Job):
    async for kind, payload in job.follow():
        if kind == "line":
            yield ServerSentEvent(raw_data=line_html(payload), event="line")
        else:
            yield ServerSentEvent(raw_data=end_html(job), event="end")


def _job(job_id: str, panel: Panel = Depends(get_panel)) -> Job:
    job = panel.jobs.get(job_id)
    if not job:
        raise HTTPException(404, t("msg.job_expired"))
    return job


@router.get("/jobs")
def jobs(request: Request, panel: Panel = Depends(get_panel)):
    return render(request, "jobs/list.html", title=t("nav.jobs"), jobs=panel.jobs.list())


@router.get("/jobs/table")
def jobs_table(request: Request, panel: Panel = Depends(get_panel)):
    return render(request, "jobs/_table.html", jobs=panel.jobs.list())


@router.get("/jobs/{job_id}")
def job_page(request: Request, job: Job = Depends(_job)):
    return render(request, "jobs/view.html", title=job.title, job=job)


@router.get("/jobs/{job_id}/stream", response_class=EventSourceResponse)
async def job_stream(job: Job = Depends(_job)):
    async for event in job_sse(job):
        yield event
