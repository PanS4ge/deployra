"""Trwała rejestracja zadań w panelu: zapis do bazy danych i odtwarzanie po restarcie."""

from __future__ import annotations

import json
import time
from typing import Any

from deployra_common.jobs import Job, JobFn, JobRegistry

from ..db import Database


class PersistentJobRegistry(JobRegistry):
    def __init__(self, db: Database, keep: int = 200) -> None:
        super().__init__(keep=keep)
        self.db = db
        self._load()

    def _load(self) -> None:
        try:
            rows = self.db.fetchall("SELECT * FROM jobs ORDER BY created ASC LIMIT ?", [self.keep])
        except Exception:
            return
        for row in rows:
            job = Job(row["kind"], row["title"])
            job.id = row["id"]
            job.status = row["status"]
            job.error = row.get("error")
            job.created = float(row.get("created") or 0)
            job.finished = float(row.get("finished")) if row.get("finished") else None
            try:
                job.lines = json.loads(row.get("lines") or "[]")
            except Exception:
                job.lines = (row.get("lines") or "").splitlines()
            if row.get("result"):
                try:
                    job.result = json.loads(row["result"])
                except Exception:
                    job.result = row["result"]
            # Przerwane zadania (serwer zrestartowany w trakcie ich trwania)
            if job.status in ("running", "queued"):
                job.status = "error"
                job.error = "Przerwane przez restart panelu"
                job.finished = job.finished or time.time()
                job.lines.append("✖ Przerwane przez restart panelu")
            self._jobs[job.id] = job

    def save_job(self, job: Job) -> None:
        try:
            with job._lock:
                lines = list(job.lines[-500:])
            lines_json = json.dumps(lines, ensure_ascii=False)
            result_json = (
                json.dumps(job.result, ensure_ascii=False)
                if job.result and isinstance(job.result, (dict, list, str, int, float, bool))
                else None
            )
            self.db.execute(
                """INSERT OR REPLACE INTO jobs (id, kind, title, status, error, result, lines, created, finished)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [job.id, job.kind, job.title, job.status, job.error, result_json, lines_json, job.created, job.finished],
            )
        except Exception:
            pass

    def start(self, kind: str, title: str, fn: JobFn) -> Job:
        job = super().start(kind, title, fn)
        self.save_job(job)
        return job

    async def _run(self, job: Job, fn: JobFn) -> None:
        self.save_job(job)
        try:
            await super()._run(job, fn)
        finally:
            self.save_job(job)
            try:
                self.db.execute(
                    "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM (SELECT id FROM jobs ORDER BY created DESC LIMIT ?) AS keep)",
                    [self.keep],
                )
            except Exception:
                pass
