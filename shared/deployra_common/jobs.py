"""Zadania w tle z logiem na żywo (do SSE).

Długie operacje (pull, build, compose up, wystawienie certyfikatu, deploy)
dostają identyfikator; klient śledzi postęp przez `Job.follow()`.
Funkcja zadania może być synchroniczna (uruchamiana w wątku) lub async.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any

MAX_LINES = 5000


class JobFailed(Exception):
    """Czytelny błąd zadania (bez śladu stosu w logu)."""


class Job:
    def __init__(self, kind: str, title: str) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.title = title
        self.status = "queued"
        self.lines: list[str] = []
        self.dropped = 0
        self.result: Any = None
        self.error: str | None = None
        self.created = time.time()
        self.finished: float | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond: asyncio.Condition | None = None

    @property
    def done(self) -> bool:
        return self.status in ("ok", "error")

    def _bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._cond = asyncio.Condition()

    def log(self, text: Any) -> None:
        """Dopisuje linie do logu; bezpieczne z wątku i z pętli zdarzeń."""
        new = str(text).replace("\r\n", "\n").split("\n")
        with self._lock:
            self.lines.extend(line.rstrip("\r") for line in new)
            overflow = len(self.lines) - MAX_LINES
            if overflow > 0:
                del self.lines[:overflow]
                self.dropped += overflow
        self._notify()

    def _notify(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self._notify_async())
        else:
            asyncio.run_coroutine_threadsafe(self._notify_async(), loop)

    async def _notify_async(self) -> None:
        assert self._cond is not None
        async with self._cond:
            self._cond.notify_all()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "created": self.created,
            "finished": self.finished,
            "lines": len(self.lines) + self.dropped,
        }

    async def follow(self, start: int = 0) -> AsyncIterator[tuple[str, Any]]:
        """Zwraca („line”, tekst) dla każdej linii (także historycznych), na końcu („end”, snapshot)."""
        position = start
        while True:
            with self._lock:
                pending = self.lines[max(position - self.dropped, 0) :]
                position = self.dropped + len(self.lines)
            for line in pending:
                yield "line", line
            if self.done:
                yield "end", self.snapshot()
                return
            assert self._cond is not None
            async with self._cond:
                await self._cond.wait_for(lambda: self.dropped + len(self.lines) > position or self.done)


JobFn = Callable[[Job], Any]


class JobRegistry:
    def __init__(self, keep: int = 200) -> None:
        self.keep = keep
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    def start(self, kind: str, title: str, fn: JobFn) -> Job:
        """Uruchamia zadanie. Wołać z pętli zdarzeń (np. z handlera FastAPI)."""
        loop = asyncio.get_running_loop()
        job = Job(kind, title)
        job._bind(loop)
        self._jobs[job.id] = job
        while len(self._jobs) > self.keep:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if not oldest.done:
                break
            self._jobs.pop(oldest_id)
        task = loop.create_task(self._run(job, fn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, fn: JobFn) -> None:
        job.status = "running"
        await job._notify_async()
        try:
            if inspect.iscoroutinefunction(fn):
                job.result = await fn(job)
            else:
                result = await asyncio.to_thread(fn, job)
                # `lambda job: coro(...)` nie jest funkcją async, ale zwraca korutynę — trzeba na nią poczekać
                job.result = await result if inspect.isawaitable(result) else result
            job.status = "ok"
        except JobFailed as exc:
            job.error = str(exc)
            job.log(f"✖ {exc}")
            job.status = "error"
        except Exception as exc:  # noqa: BLE001 — błąd zadania trafia do jego logu
            job.error = f"{type(exc).__name__}: {exc}"
            job.log(traceback.format_exc().rstrip())
            job.status = "error"
        finally:
            job.finished = time.time()
            await job._notify_async()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return list(reversed(self._jobs.values()))
