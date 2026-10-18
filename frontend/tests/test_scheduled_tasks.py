import asyncio
import time
from datetime import datetime

import httpx
import pytest
from deployra_common.client import RemoteError
from deployra_panel.services import cron, scheduled_tasks

# 2026-10-18 jest niedzielą, 2026-10-19 poniedziałkiem


class ExecNode:
    """Węzeł w pamięci: jeden kontener aplikacji `blog` i odpowiedź na `exec/run`."""

    def __init__(self, reply=None, error=None, containers=True, delay=0.0):
        self.reply, self.error, self.delay = reply, error, delay
        self.containers = containers
        self.calls = []

    async def get(self, path, **_):
        if not self.containers:
            return []
        return [{"id": "c1", "Id": "c1", "name": "app-blog", "app": "blog", "state": "running", "ports": []}]

    async def post(self, path, json=None, **kwargs):
        self.calls.append((path, json, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


OK_REPLY = {"exit_code": 0, "output": "done\n", "timed_out": False, "duration": 1.2}


@pytest.fixture
def app_row(admin):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired",
                              secret=panel.sealer.seal(b"x" * 32))
    app_id = panel.db.insert("apps", slug="blog", name="Blog", node_id=node_id, kind="image", status="running", spec="{}")
    return panel.db.get("apps", app_id)


def make_task(panel, app_id, schedule="*/5 * * * *", enabled=True, timeout="300"):
    data = scheduled_tasks.clean("Sync", "php artisan schedule:run", schedule, timeout, enabled)
    return scheduled_tasks.save(panel, app_id, data)


# --- parser cron -----------------------------------------------------------------------


def test_cron_steps_ranges_and_lists():
    assert cron.matches("*/5 * * * *", datetime(2026, 10, 19, 14, 35))
    assert not cron.matches("*/5 * * * *", datetime(2026, 10, 19, 14, 36))
    # zakres z krokiem: godziny 9, 11, 13, 15, 17 — tylko w dni robocze
    assert cron.matches("0 9-17/2 * * 1-5", datetime(2026, 10, 19, 11, 0))
    assert not cron.matches("0 9-17/2 * * 1-5", datetime(2026, 10, 19, 12, 0))
    assert not cron.matches("0 9-17/2 * * 1-5", datetime(2026, 10, 18, 11, 0))  # niedziela
    assert cron.matches("15,45 * * * *", datetime(2026, 10, 19, 3, 45))
    assert not cron.matches("15,45 * * * *", datetime(2026, 10, 19, 3, 30))
    # `od/krok` sięga do końca pola: 5, 20, 35, 50
    assert cron.matches("5/15 * * * *", datetime(2026, 10, 19, 3, 50))
    assert not cron.matches("5/15 * * * *", datetime(2026, 10, 19, 3, 55))


def test_cron_presets_and_sunday():
    assert cron.matches("@hourly", datetime(2026, 10, 19, 7, 0))
    assert not cron.matches("@hourly", datetime(2026, 10, 19, 7, 1))
    assert cron.matches("@daily", datetime(2026, 10, 19, 0, 0))
    assert not cron.matches("@daily", datetime(2026, 10, 19, 1, 0))
    assert cron.matches("@weekly", datetime(2026, 10, 18, 0, 0))  # niedziela
    assert not cron.matches("@weekly", datetime(2026, 10, 19, 0, 0))
    assert cron.matches("@monthly", datetime(2026, 11, 1, 0, 0))
    assert not cron.matches("@monthly", datetime(2026, 11, 2, 0, 0))
    assert cron.matches("0 0 * * 7", datetime(2026, 10, 18, 0, 0))  # 7 też oznacza niedzielę


def test_cron_day_of_month_or_weekday():
    # gdy oba pola dnia są ograniczone, wystarczy jedno: 13. dzień miesiąca albo piątek
    assert cron.matches("0 0 13 * 5", datetime(2026, 10, 13, 0, 0))  # wtorek 13.
    assert cron.matches("0 0 13 * 5", datetime(2026, 10, 16, 0, 0))  # piątek
    assert not cron.matches("0 0 13 * 5", datetime(2026, 10, 14, 0, 0))  # środa 14.


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "60 * * * *", "*/0 * * * *", "5-1 * * * *", "a * * * *",
                                  "1,,2 * * * *", "* * * * 8", "* * 0 * *", "@yearly", "/5 * * * *", "-5 * * * *"])
def test_cron_rejects_invalid(expr):
    with pytest.raises(ValueError):
        cron.validate(expr)


def test_cron_accepts_extra_spaces():
    cron.validate("  */10   *  * * 1-5 ")
    assert cron.matches("  @daily ", datetime(2026, 10, 19, 0, 0))


# --- walidacja i baza ------------------------------------------------------------------


def test_clean_validates_form_values():
    assert scheduled_tasks.clean(" Sync ", " echo 1 ", "*/5   * * * *", "120", True) == {
        "name": "Sync", "command": "echo 1", "schedule": "*/5 * * * *", "timeout_s": 120, "enabled": 1}
    assert scheduled_tasks.clean("n", "x", "@daily", "300", False)["enabled"] == 0
    bad = [("", "x", "@daily", "300"), ("n", "  ", "@daily", "300"), ("n", "x", "bad", "300"), ("n", "x", "@daily", "30"),
           ("n", "x", "@daily", "99999"), ("n", "x", "@daily", "abc"), ("n", "x", "@daily", None), ("n" * 61, "x", "@daily", "300")]
    for name, command, schedule, timeout in bad:
        with pytest.raises(ValueError):
            scheduled_tasks.clean(name, command, schedule, timeout, True)


def test_migration_17_creates_tables(panel):
    assert panel.db.scalar("SELECT COUNT(*) FROM schema_version WHERE version = 17") == 1
    assert panel.db.fetchall("SELECT * FROM scheduled_tasks") == []
    assert panel.db.fetchall("SELECT * FROM scheduled_task_runs") == []


def test_runs_are_trimmed_to_last_fifty(panel, app_row):
    task_id = make_task(panel, app_row["id"])
    for i in range(55):
        panel.db.insert("scheduled_task_runs", task_id=task_id, started_at=float(i), status="ok", output=str(i))
    scheduled_tasks.trim_runs(panel, task_id)
    kept = scheduled_tasks.runs(panel, task_id)
    assert len(kept) == 50 and kept[0]["output"] == "54" and kept[-1]["output"] == "5"


def test_remove_for_app_drops_tasks_and_history(panel, app_row):
    task_id = make_task(panel, app_row["id"])
    panel.db.insert("scheduled_task_runs", task_id=task_id, started_at=1.0, status="ok", output="")
    scheduled_tasks.remove_for_app(panel, app_row["id"])
    assert scheduled_tasks.list_tasks(panel, app_row["id"]) == []
    assert panel.db.fetchall("SELECT * FROM scheduled_task_runs") == []


# --- uruchamianie ----------------------------------------------------------------------


def test_execute_records_successful_run(panel, app_row, monkeypatch):
    node = ExecNode(reply=OK_REPLY)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    task_id = make_task(panel, app_row["id"], timeout="600")
    asyncio.run(scheduled_tasks.execute(panel, task_id))

    run = scheduled_tasks.runs(panel, task_id)[0]
    assert run["status"] == "ok" and run["exit_code"] == 0 and run["output"] == "done\n" and run["duration_s"] is not None
    task = panel.db.get("scheduled_tasks", task_id)
    assert task["last_status"] == "ok" and task["last_run_at"] is not None
    path, body, kwargs = node.calls[0]
    assert path == "/api/containers/c1/exec/run?ep=local"
    assert body == {"cmd": ["sh", "-c", "php artisan schedule:run"], "timeout": 600}
    assert kwargs["timeout"] == 660  # limit zadania + zapas na sieć
    assert task_id not in panel.task_runs


@pytest.mark.parametrize(("reply", "error", "status", "code"), [
    ({"exit_code": 3, "output": "boom", "timed_out": False}, None, "failed", 3),
    ({"exit_code": 137, "output": "", "timed_out": True}, None, "timeout", 137),
    (None, httpx.ReadTimeout("slow"), "timeout", None),
    (None, RemoteError(409, "container is not running"), "failed", None),
])
def test_execute_maps_outcomes(panel, app_row, monkeypatch, reply, error, status, code):
    node = ExecNode(reply=reply, error=error)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    task_id = make_task(panel, app_row["id"])
    asyncio.run(scheduled_tasks.execute(panel, task_id))
    run = scheduled_tasks.runs(panel, task_id)[0]
    assert run["status"] == status and run["exit_code"] == code
    if error is not None and not isinstance(error, httpx.TimeoutException):
        assert "not running" in run["output"]


def test_execute_without_container_fails_cleanly(panel, app_row, monkeypatch):
    node = ExecNode(containers=False)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    task_id = make_task(panel, app_row["id"])
    asyncio.run(scheduled_tasks.execute(panel, task_id))
    run = scheduled_tasks.runs(panel, task_id)[0]
    assert run["status"] == "failed" and node.calls == []


def test_output_is_cut_to_sixteen_kilobytes(panel, app_row, monkeypatch):
    node = ExecNode(reply={"exit_code": 0, "output": "a" * (40 * 1024), "timed_out": False})
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    task_id = make_task(panel, app_row["id"])
    asyncio.run(scheduled_tasks.execute(panel, task_id))
    assert len(scheduled_tasks.runs(panel, task_id)[0]["output"]) == scheduled_tasks.OUTPUT_LIMIT


def test_start_refuses_second_run_while_first_is_going(panel, app_row, monkeypatch):
    node = ExecNode(reply=OK_REPLY, delay=0.05)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    task_id = make_task(panel, app_row["id"])

    async def scenario():
        assert scheduled_tasks.start(panel, task_id) is True
        assert scheduled_tasks.start(panel, task_id) is False
        await panel.task_runs[task_id]

    asyncio.run(scenario())
    assert task_id not in panel.task_runs and len(node.calls) == 1


def test_tick_runs_only_due_enabled_tasks_of_running_apps(panel, app_row, monkeypatch):
    node = ExecNode(reply=OK_REPLY)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    due = make_task(panel, app_row["id"], schedule="*/5 * * * *")
    make_task(panel, app_row["id"], schedule="0 3 * * *")  # nie teraz
    make_task(panel, app_row["id"], schedule="*/5 * * * *", enabled=False)  # wyłączone

    async def scenario(when):
        started = scheduled_tasks.tick(panel, when)
        for task_id in started:
            await panel.task_runs[task_id]
        return started

    assert asyncio.run(scenario(datetime(2026, 10, 19, 10, 5))) == [due]
    panel.db.update("apps", app_row["id"], status="stopped")  # zatrzymana aplikacja: harmonogram jej nie rusza
    assert asyncio.run(scenario(datetime(2026, 10, 19, 10, 10))) == []


# --- trasy -----------------------------------------------------------------------------


def test_task_routes_roundtrip(admin, app_row, monkeypatch):
    panel = admin.app.state.panel
    base = f"/apps/{app_row['id']}/tasks"
    page = admin.get(base)
    assert page.status_code == 200 and 'id="task-list"' in page.text

    created = admin.post(base, data={"name": "Nightly", "command": "php artisan schedule:run", "schedule": "0 2 * * *",
                                     "timeout": "600", "enabled": "1", "preset": "0 0 * * *"})  # fmt: skip
    assert created.status_code == 200 and "tasks-changed" in created.headers["HX-Trigger"]
    task = panel.db.fetchone("SELECT * FROM scheduled_tasks WHERE app_id = ?", [app_row["id"]])
    assert task["name"] == "Nightly" and task["timeout_s"] == 600 and task["enabled"] == 1

    listing = admin.get(f"{base}/list").text
    assert "Nightly" in listing and "php artisan schedule:run" in listing

    bad = admin.post(base, data={"name": "X", "command": "x", "schedule": "every day", "timeout": "300", "enabled": "1"})
    assert bad.status_code == 400 and panel.db.scalar("SELECT COUNT(*) FROM scheduled_tasks") == 1

    assert admin.post(f"{base}/{task['id']}/toggle").status_code == 200
    assert panel.db.get("scheduled_tasks", task["id"])["enabled"] == 0

    upd = admin.post(f"{base}/{task['id']}/update", data={"name": "Nightly 2", "command": "true", "schedule": "@daily", "timeout": "300"})
    row = panel.db.get("scheduled_tasks", task["id"])
    assert upd.status_code == 200 and row["name"] == "Nightly 2" and row["schedule"] == "@daily" and row["enabled"] == 0

    assert admin.get(f"{base}/{task['id']}/runs").status_code == 200  # bez przebiegów
    panel.db.insert("scheduled_task_runs", task_id=task["id"], started_at=time.time(), duration_s=1.5, exit_code=0, status="ok", output="hello-run")
    history = admin.get(f"{base}/{task['id']}/runs").text
    assert "hello-run" in history and "1 s" in history  # wynik i czas trwania w historii
    assert admin.post(f"{base}/9999/toggle").status_code == 404

    assert admin.post(f"{base}/{task['id']}/delete").status_code == 200
    assert panel.db.get("scheduled_tasks", task["id"]) is None


def test_run_now_executes_in_background(admin, app_row, monkeypatch):
    panel = admin.app.state.panel
    node = ExecNode(reply=OK_REPLY)
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    task_id = make_task(panel, app_row["id"])

    response = admin.post(f"/apps/{app_row['id']}/tasks/{task_id}/run")
    assert response.status_code == 200 and "tasks-changed" in response.headers["HX-Trigger"]
    deadline = time.time() + 5
    while time.time() < deadline and not scheduled_tasks.runs(panel, task_id):
        time.sleep(0.05)
    deadline = time.time() + 5
    while time.time() < deadline and panel.db.get("scheduled_tasks", task_id)["last_status"] is None:
        time.sleep(0.05)
    assert scheduled_tasks.runs(panel, task_id)[0]["status"] == "ok"
