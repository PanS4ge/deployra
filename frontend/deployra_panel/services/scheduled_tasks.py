"""Zadania cykliczne w kontenerach aplikacji: zapis, uruchamianie (`docker exec` na węźle) i harmonogram co minutę."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

from deployra_common import i18n as i18n_core

from ..db import now
from ..i18n import t
from . import cron

MAX_RUNS = 50  # historia: ostatnie przebiegi każdego zadania
OUTPUT_LIMIT = 16 * 1024  # w historii zostaje ogon wyniku
NAME_MAX, COMMAND_MAX = 60, 2000
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 60, 36000, 300
PAUSED_APP_STATES = ("stopped", "paused")  # takich aplikacji harmonogram nie rusza


def clean(name: str, command: str, schedule: str, timeout: Any, enabled: bool) -> dict[str, Any]:
    """Walidacja formularza zadania; błędy idą do użytkownika jako ValueError (toast 400)."""
    name, command = (name or "").strip(), (command or "").strip()
    schedule = " ".join((schedule or "").split())
    if not name or len(name) > NAME_MAX:
        raise ValueError(t("tasks.bad_name"))
    if not command or len(command) > COMMAND_MAX:
        raise ValueError(t("tasks.bad_command"))
    try:
        cron.validate(schedule)
    except ValueError:
        raise ValueError(t("tasks.bad_schedule")) from None
    try:
        seconds = int(timeout)
    except (TypeError, ValueError):
        raise ValueError(t("tasks.bad_timeout")) from None
    if not TIMEOUT_MIN <= seconds <= TIMEOUT_MAX:
        raise ValueError(t("tasks.bad_timeout"))
    return {"name": name, "command": command, "schedule": schedule, "timeout_s": seconds, "enabled": 1 if enabled else 0}


def list_tasks(panel: Any, app_id: int) -> list[dict[str, Any]]:
    return panel.db.fetchall("SELECT * FROM scheduled_tasks WHERE app_id = ? ORDER BY id", [app_id])


def get_task(panel: Any, app_id: int, task_id: int) -> dict[str, Any] | None:
    return panel.db.fetchone("SELECT * FROM scheduled_tasks WHERE id = ? AND app_id = ?", [task_id, app_id])


def runs(panel: Any, task_id: int) -> list[dict[str, Any]]:
    return panel.db.fetchall("SELECT * FROM scheduled_task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?", [task_id, MAX_RUNS])


def save(panel: Any, app_id: int, data: dict[str, Any], task_id: int | None = None) -> int:
    if task_id is None:
        return panel.db.insert("scheduled_tasks", app_id=app_id, created_at=now(), **data)
    panel.db.update("scheduled_tasks", task_id, **data)
    return task_id


def remove(panel: Any, task_id: int) -> None:
    cancel(panel, task_id)
    panel.db.execute("DELETE FROM scheduled_task_runs WHERE task_id = ?", [task_id])
    panel.db.delete("scheduled_tasks", task_id)


def remove_for_app(panel: Any, app_id: int) -> None:
    for task in list_tasks(panel, app_id):
        remove(panel, task["id"])


def cancel(panel: Any, task_id: int) -> None:
    running = panel.task_runs.get(task_id)
    if running:
        running.cancel()


def start(panel: Any, task_id: int) -> bool:
    """Uruchamia przebieg w tle; False, gdy poprzedni przebieg tego zadania jeszcze trwa. Wymaga działającej pętli."""
    if task_id in panel.task_runs:
        return False
    job = asyncio.create_task(execute(panel, task_id))
    panel.task_runs[task_id] = job
    # wpis znika także wtedy, gdy zadanie przerwano zanim ruszyło (wtedy `finally` w execute się nie wykona)
    job.add_done_callback(lambda done: panel.task_runs.pop(task_id, None) if panel.task_runs.get(task_id) is done else None)
    return True


def trim_runs(panel: Any, task_id: int) -> None:
    panel.db.execute(
        "DELETE FROM scheduled_task_runs WHERE task_id = ? AND id NOT IN "
        "(SELECT id FROM (SELECT id FROM scheduled_task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?) AS keep)",
        [task_id, task_id, MAX_RUNS],
    )


async def _run_in_container(panel: Any, task: dict[str, Any]) -> tuple[str, int | None, str]:
    """(status, kod wyjścia, wynik) — ten sam dobór węzła i kontenera co akcje aplikacji."""
    from ..routes.apps import app_container
    from ..routes.docker import _p

    app = panel.db.get("apps", task["app_id"])
    if not app:
        return "failed", None, t("tasks.no_app")
    node, ep, cid = await app_container(panel, app)
    if not node or not cid:
        return "failed", None, t("tasks.no_container", name=app["name"])
    body = {"cmd": ["sh", "-c", task["command"]], "timeout": task["timeout_s"]}
    res = await panel.node_client(node).post(_p(f"/api/containers/{cid}/exec/run", ep), json=body, timeout=task["timeout_s"] + 60)
    code = int((res or {}).get("exit_code", 1))
    status = "timeout" if (res or {}).get("timed_out") else "ok" if code == 0 else "failed"
    return status, code, (res or {}).get("output", "")


async def execute(panel: Any, task_id: int) -> None:
    """Jeden przebieg zadania; wynik (także błąd) ląduje w historii."""
    task = panel.db.get("scheduled_tasks", task_id)
    if not task:
        return
    started = time.time()
    run_id = panel.db.insert("scheduled_task_runs", task_id=task_id, started_at=started, status="running", output="")
    status, code, output = "failed", None, ""
    try:
        status, code, output = await _run_in_container(panel, task)
    except httpx.TimeoutException:
        status, output = "timeout", t("tasks.run_timeout")
    except asyncio.CancelledError:
        output = t("tasks.cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 — błąd węzła trafia do historii, pętla dalej działa
        output = str(getattr(exc, "detail", None) or exc)
        logger.warning(f"Zadanie {task_id}: {type(exc).__name__}: {exc}")
    finally:
        panel.db.update("scheduled_task_runs", run_id, status=status, exit_code=code, duration_s=round(time.time() - started, 2),
                        output=(output or "")[-OUTPUT_LIMIT:])  # fmt: skip
        panel.db.update("scheduled_tasks", task_id, last_run_at=started, last_status=status)
        trim_runs(panel, task_id)


def tick(panel: Any, when: datetime) -> list[int]:
    """Odpala zadania pasujące do minuty `when`; zwraca ich ID."""
    due = []
    rows = panel.db.fetchall(
        "SELECT t.id, t.schedule FROM scheduled_tasks t JOIN apps a ON a.id = t.app_id "
        "WHERE t.enabled = 1 AND a.status NOT IN (?, ?)", list(PAUSED_APP_STATES),
    )  # fmt: skip
    for row in rows:
        try:
            matched = cron.matches(row["schedule"], when)
        except ValueError:
            continue
        if matched and start(panel, row["id"]):
            due.append(row["id"])
    return due


async def loop(panel: Any) -> None:
    """Pętla harmonogramu: sprawdza raz na minutę, na początku każdej minuty."""
    i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)
    last = datetime.now().replace(second=0, microsecond=0)  # minuty startu nie odpalamy — harmonogram od następnej
    try:
        while True:
            await asyncio.sleep(60 - time.time() % 60 + 0.5)
            current = datetime.now().replace(second=0, microsecond=0)
            if current == last:
                continue
            last = current
            try:
                tick(panel, current)
            except Exception as exc:  # noqa: BLE001 — pętla harmonogramu nie może paść
                logger.error(f"Zadania cykliczne: {type(exc).__name__}: {exc}")
    finally:
        for task_id in list(panel.task_runs):
            cancel(panel, task_id)
